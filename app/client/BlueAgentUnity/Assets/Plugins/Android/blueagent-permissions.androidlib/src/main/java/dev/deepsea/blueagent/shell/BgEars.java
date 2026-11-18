package dev.deepsea.blueagent.shell;

import android.content.Context;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaPlayer;
import android.net.Uri;
import android.os.SystemClock;
import android.util.Base64;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;

/**
 * **熄屏時的耳朵**（Unity 版才有的一支；原 app 的收音本來就全在 Kotlin，熄屏照跑）。
 *
 * 為什麼要它：Unity 的 activity 一 onStop，`UnityPlayer` 就把主迴圈停掉（`pauseUnity`）——
 * C# 的 `Microphone` 輪詢與 `UnityWebRequest` 上行全停。前景服務保得住行程、保不住這些。
 * 所以熄屏那段由這裡接手，**做的事與 C# `EarsLink` 一模一樣、只是換成原生**：
 * <ul>
 *   <li>收音：`AudioRecord`（音源＝設定 `audio_source`，{@link MicFx}；平台 NS／AGC／AEC 照前景那條掛；
 *       16 kHz、單聲道、PCM16）30 ms 一塊 ⇒ 帶通（`VoiceBand`，同 `MicCapture`）⇒ 3 s 環形 ⇒ 喚醒網；</li>
 *   <li>**呼びかけ模式**（{@link BgWake}，2026-10-18 加）：安靜時一塊都不送；本機 KWS（＋①聲紋閘）叫到名字 ⇒
 *       預開閘：`seed=1`（環形 3 s）＋之後每 0.5 s `pre=1`，等橋的 `gate`（POST 回應或 SSE `ears{kind:gate}`，
 *       只認先到的）⇒ 開門＝10 s 會話窗（橋說 `heard`／判 `his` 才續期）、拒絕＝回到等名字。節奏＝{@link BgTurn}
 *       （共用層 `EarsStream` 的移植），與前景 `EarsLink` 同一套。自動（toggle）模式／網起不來 ⇒ 一直送（舊行為）；</li>
 *   <li>上行：`POST /v1/ears/stream?session…&seed=1&pre=1`，一次只飛一塊（自己一條執行緒，不擋收音）；</li>
 *   <li>訂閱：`GET /v1/ears/live?session&token`（她的回覆從這裡回來）；`speech` 幀的 WAV 依序播出，
 *       播的時候不上傳（閥門——不然橋聽到的是她自己的聲音），播完再關一個聾窗
 *       （{@link MicFx#deafWindowMs}：AEC 掛上 ⇒ 0，否則 350／900 ms）；</li>
 *   <li>每一幀（`speech` 拿掉 `audio`）照樣丟進 {@link Native} 的事件佇列 ⇒ 亮屏後 C# 補進紀錄／主頁，
 *       而不會再念一次。</li>
 * </ul>
 * 亮屏（activity onStart）立刻停：錄音、訂閱、播放全收掉，交還給 C#。
 */
final class BgEars {
    private static final String TAG = Native.TAG;
    private static final int RATE = 16000;
    private static final int RING = RATE * 3;                // 3 s（`MicCapture.RingSeconds`）：seed 就是它
    private static final int BLOCK = RATE * BgTurn.STREAM_BLOCK_MS / 1000;   // 0.5 s：seed 至少要這麼長
    private static final long START_DELAY_MS = 600;          // 讓 Unity 先把自己的麥克風放掉
    private static final long THINK_HOLD_MS = 60000L;        // `EARS_THINK_HOLD_MS`
    private static final long PROBE_MS = 30000L;

    private static volatile boolean wanted;
    private static volatile String session = "", soul = "", mode = "";
    private static volatile boolean tts = true;
    static volatile float volume = 1f;
    /** 應答音那組 wav 的資料夾（{@link Native#setAck}）；空＝不播。 */
    static volatile String ackDir = "";
    /** 正在播的應答音（留個參照：只掛在區域變數上的 MediaPlayer 可能被 GC 收掉、半路沒聲）。 */
    private static MediaPlayer ackPlayer;
    /** 喚醒網的設定（`Native.setWake`）。 */
    static volatile BgWake.Config wake = new BgWake.Config();
    /** 帶通（`Native.setBand`；`voice_filter`／`voice_high_pass_hz`／`voice_low_pass_hz`）。 */
    static volatile boolean bandOn = true;
    static volatile double bandHigh = 100.0, bandLow = 7200.0;

    private static volatile boolean running;
    private static volatile int generation;
    private static AudioRecord record;
    private static MicFx fx;
    private static volatile HttpURLConnection live;
    private static final Runnable START = BgEars::start;

    // 喚醒網（麥克風執行緒上建／餵；跨熄屏留著，設定沒變就不重建——sherpa 載一次 0.2–0.6 s）
    private static final Object NET_LOCK = new Object();
    private static BgWake net;

    // 回合（LOCK 保護：麥克風、上行、訂閱、閘門四條執行緒都會碰）
    private static final Object LOCK = new Object();
    private static final BgTurn TURN = new BgTurn();
    private static final short[] ring = new short[RING];
    private static int ringWrite, ringFilled;
    private static long total, cursor;
    private static boolean seedPending, gateHandled, valveShut, useGate, blockPre, thinking;
    private static Boolean preopenSupported;
    private static long lastSentAt;

    // 播放（主執行緒）
    private static final ArrayDeque<File> QUEUE = new ArrayDeque<>();
    private static MediaPlayer player;
    /** 這個 `player` 正在播（或準備中）的那一檔：{@link #park} で前景に戻るとき「どこまで鳴ったか」と一緒に Unity へ返す。 */
    private static File playing;
    private static volatile boolean speaking;
    private static volatile long valveUntil;
    private static int clipSeq;

    private BgEars() {}

    static void configure(boolean on, String s, String so, String m, boolean t) {
        wanted = on;
        session = s == null ? "" : s;
        soul = so == null ? "" : so;
        mode = m == null ? "" : m;
        tts = t;
        if (!on && running) Native.MAIN.post(BgEars::stop);
    }

    /**
     * 「暫停」：app 自己開了全螢幕的系統畫面（相册選圖）⇒ activity 雖然 onStop、**不是熄屏**，不要起耳朵
     * （外部評審 P1-3，2026-10-18：翻相册的整段時間都在收音、上行）。{@link PickerActivity} 起手設、onDestroy 清。
     */
    static volatile boolean paused;

    static void onBackground() {
        if (!wanted || paused) return;
        Native.MAIN.removeCallbacks(START);
        Native.MAIN.postDelayed(START, START_DELAY_MS);
    }

    static void onForeground() {
        Native.MAIN.removeCallbacks(START);
        if (running) stop();
        park();                                   // 鳴り終わらなかった分は Unity に返す（{@link #takeBack}）
    }

    private static long now() {
        return SystemClock.elapsedRealtime();
    }

    private static String query(boolean seed, boolean pre) {
        StringBuilder sb = new StringBuilder("?session=").append(Uri.encode(session));
        if (!soul.isEmpty()) sb.append("&soul=").append(Uri.encode(soul));
        if (!mode.isEmpty()) sb.append("&mode=").append(Uri.encode(mode));
        sb.append("&tts=").append(tts ? "1" : "0");
        if (seed) sb.append("&seed=1");
        if (pre) sb.append("&pre=1");
        return sb.toString();
    }

    @SuppressWarnings("MissingPermission")
    private static void start() {
        if (paused) return;                       // 相册選圖那段不是熄屏（見 `paused`）
        if (running || !wanted || Native.app == null) return;
        if (Native.bridgeUrl.isEmpty() || Native.bridgeToken.isEmpty() || session.isEmpty()) {
            Log.i(TAG, "熄屏耳朵：橋／session 沒設定 ⇒ 不開");
            return;
        }
        int min = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        AudioRecord r;
        try {
            r = new AudioRecord(MicFx.sourceId(MicFx.source), RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, Math.max(min, RATE * 2 * 4));
            if (r.getState() != AudioRecord.STATE_INITIALIZED) throw new IllegalStateException("AudioRecord 沒初始化");
            r.startRecording();
        } catch (Exception e) {
            Log.i(TAG, "熄屏耳朵：開不了麥克風 " + e.getMessage());
            return;
        }
        record = r;
        fx = new MicFx(r, MicFx.effects);
        String attached = MicFx.joined(fx.attach());
        running = true;
        final int gen = ++generation;
        BgWake.Config cfg = wake;
        synchronized (LOCK) {
            ringWrite = 0;
            ringFilled = 0;
            total = 0;
            cursor = 0;
            valveShut = false;
            thinking = false;
            gateHandled = true;
            preopenSupported = null;
            // 網還沒建（麥克風執行緒建）⇒ 先當「呼びかけ」：等名字、不送；建不起來再退回一直送（見 listen）。
            useGate = cfg.on;
            TURN.close();
            seedPending = false;
            lastSentAt = now() - BgTurn.STREAM_BLOCK_MS;
        }
        Log.i(TAG, "熄屏耳朵：開（session=" + session + "／soul=" + soul + "／mode=" + mode
            + "／source=" + MicFx.source + "／" + fx.describe() + " 掛上=" + attached
            + "／帶通 " + (bandOn ? (int) bandHigh + "–" + (int) bandLow + " Hz" : "關")
            + "／" + (cfg.on ? "呼びかけ（等名字）" : "一直送") + "）");
        new Thread(() -> listen(r, gen, cfg), "ba-bg-ears-mic").start();
        new Thread(() -> uplink(gen), "ba-bg-ears-up").start();
        new Thread(() -> subscribe(gen), "ba-bg-ears-live").start();
    }

    private static void stop() {
        if (!running) return;
        running = false;
        generation++;
        AudioRecord r = record;
        record = null;
        if (r != null) {
            try { r.stop(); } catch (Exception ignored) {}
        }
        // 效果握著 session ⇒ 要在收音執行緒 `r.release()` 之前放掉（`MicEffects.release` 的註解）。
        MicFx f = fx;
        fx = null;
        if (f != null) f.release();
        HttpURLConnection c = live;
        live = null;
        if (c != null) {
            new Thread(() -> { try { c.disconnect(); } catch (Exception ignored) {} }).start();
        }
        Log.i(TAG, "熄屏耳朵：關（交還給 Unity）");
    }

    // ────────────────────────────────────────────────────────── 收音＋喚醒網（麥克風執行緒）

    /** 照設定把網備好（`WakeNet.Ensure`）：設定沒變就沿用上一次熄屏那顆。回 null＝這一輪不用網。 */
    private static BgWake ensureNet(BgWake.Config cfg) {
        if (!cfg.on) return null;
        if (!cfg.usable) {
            Log.i(TAG, "熄屏喚醒網：前景那顆網沒起來／自檢沒過（" + cfg.character + "）⇒ 不用");
            return null;
        }
        if (net != null && net.matches(cfg)) {
            net.reset();
            return net;
        }
        if (net != null) net.close();
        net = null;
        String[] why = {""};
        BgWake made = BgWake.build(cfg, new BgWake.Listener() {
            @Override public void onWake(float kws, long at, float cos) { onLocalWake(kws, cos); }
            @Override public void onReject(float cos, float bar) {
                Log.i(TAG, "熄屏耳朵：叫到名字但聲紋判非本人（cos " + BgWake.f3(cos) + " < " + BgWake.f3(bar) + "）⇒ 不送");
            }
        }, why);
        if (made == null) Log.i(TAG, "熄屏喚醒網：起不來 ⇒ " + why[0]);
        net = made;
        return made;
    }

    private static void listen(AudioRecord r, int gen, BgWake.Config cfg) {
        BgWake w;
        synchronized (NET_LOCK) {
            w = ensureNet(cfg);
        }
        synchronized (LOCK) {
            if (cfg.on && w == null) {
                // 網起不來 ⇒ 退回一直送（不做本機兜底以外的猜測：至少她聽得到；大聲寫一行）。
                useGate = false;
                Log.i(TAG, "熄屏耳朵：沒有本機喚醒網 ⇒ 退回一直送（舊行為）");
            }
            if (!useGate) {
                TURN.arm(now());
                seedPending = true;
            }
        }
        VoiceBand band = new VoiceBand(RATE, bandHigh, bandLow, bandOn);
        short[] block = new short[BgWake.BLOCK];
        while (running && gen == generation) {
            int filled = 0;
            while (filled < block.length && running && gen == generation) {
                int n = r.read(block, filled, block.length - filled);
                if (n <= 0) break;
                filled += n;
            }
            if (filled < block.length) break;
            if (++injectPoll % 16 == 0 && inject == null) inject = loadInject();
            if (inject != null) {
                // 測試：注入的錄音取代麥克風（同前景 `MicCapture.Inject`；帶通之前換掉）。
                for (int i = 0; i < block.length; i++) block[i] = injectAt < inject.length ? inject[injectAt++] : 0;
                if (injectAt >= inject.length) {
                    inject = null;
                    Log.i(TAG, "熄屏耳朵：注入音訊放完");
                }
            }
            band.process(block);
            boolean shut = speaking || SystemClock.elapsedRealtime() < valveUntil;
            boolean hear;
            synchronized (LOCK) {
                if (shut) {
                    ringFilled = 0;                      // 閥門關著：什麼都不留（`Mic.Drop`）
                } else {
                    for (short v : block) {
                        ring[ringWrite] = v;
                        ringWrite = (ringWrite + 1) % RING;
                        if (ringFilled < RING) ringFilled++;
                    }
                    total += block.length;
                }
                hear = useGate && !shut;
            }
            // 喚醒網吃不吃這一塊：閥門關著就不餵（熄屏沒有插話：`paused && !bargeIn` ⇒ continue）。
            if (hear && w != null) {
                synchronized (NET_LOCK) {
                    if (gen == generation) w.onBlock(block);
                }
            }
        }
        try { r.release(); } catch (Exception ignored) {}
        Log.i(TAG, "熄屏耳朵：收音收工" + (w != null ? "（喚醒網 " + w.stats() + "）" : ""));
    }

    // 測試用的注入（`files/bg_inject.wav`，16 kHz 單聲道 PCM16；讀到就改名成 `.done`，一次性）
    private static short[] inject;
    private static int injectAt, injectPoll;

    /** 驗收包（C# `BA_SELFTEST`）才認 `bg_inject.wav`——正式包裡這條路是關的（外部評審 v2 A2）。 */
    static volatile boolean selfTest;

    private static short[] loadInject() {
        if (!selfTest) return null;
        Context ctx = Native.app;
        File dir = ctx == null ? null : ctx.getExternalFilesDir(null);
        File f = dir == null ? null : new File(dir, "bg_inject.wav");
        if (f == null || !f.isFile()) return null;
        try {
            byte[] b = java.nio.file.Files.readAllBytes(f.toPath());
            f.renameTo(new File(dir, "bg_inject.done.wav"));
            int at = 12;
            while (at + 8 <= b.length) {
                String id = new String(b, at, 4, StandardCharsets.US_ASCII);
                int size = (b[at + 4] & 0xFF) | (b[at + 5] & 0xFF) << 8 | (b[at + 6] & 0xFF) << 16 | (b[at + 7] & 0xFF) << 24;
                if ("data".equals(id)) {
                    int n = Math.min(size, b.length - at - 8) / 2;
                    short[] pcm = new short[n];
                    for (int i = 0; i < n; i++) pcm[i] = (short) ((b[at + 8 + i * 2] & 0xFF) | (b[at + 9 + i * 2] << 8));
                    injectAt = 0;
                    Log.i(TAG, "熄屏耳朵：注入音訊 " + f.getName() + "（" + n * 1000L / RATE + " ms）");
                    return pcm;
                }
                at += 8 + size + (size & 1);
            }
        } catch (Exception e) {
            Log.i(TAG, "熄屏耳朵：注入音訊讀不到 " + e.getMessage());
        }
        return null;
    }

    /** 環形裡最近的 n 個樣本（LOCK 內叫）。 */
    private static short[] recent(int n) {
        int take = Math.min(n, ringFilled);
        short[] out = new short[take];
        int from = (ringWrite - take + RING) % RING;
        for (int i = 0; i < take; i++) out[i] = ring[(from + i) % RING];
        return out;
    }

    /**
     * 本機喚醒網叫到名字（`EarsLink.OnLocalWake`；閘門執行緒或麥克風執行緒）：開一輪預開閘——
     * `seed`＝環形 3 s（名字在觸發之前就講了）＋之後每 0.5 s `pre=1`，等橋的 `gate`。
     * 有一塊正在飛 ⇒ 不動（那段音訊屬於上一個回合）。
     */
    private static void onLocalWake(float kws, float cos) {
        synchronized (LOCK) {
            if (!running || !useGate) return;
            if (speaking || SystemClock.elapsedRealtime() < valveUntil) {
                Log.i(TAG, "熄屏耳朵：本機喚醒（KWS " + BgWake.f3(kws) + "）但她在講 ⇒ 不開（熄屏沒有插話）");
                return;
            }
            if (TURN.inFlight) {
                Log.i(TAG, "熄屏耳朵：本機喚醒（KWS " + BgWake.f3(kws) + "）但有一塊還在飛 ⇒ 這次不開");
                return;
            }
            long now = now();
            TURN.preopenEnter(now);
            gateHandled = false;
            seedPending = true;
            lastSentAt = now - BgTurn.STREAM_BLOCK_MS;      // 立刻送第一塊
        }
        Log.i(TAG, "熄屏耳朵：本機喚醒（KWS " + BgWake.f3(kws) + "／cos " + BgWake.f3(cos)
            + "）⇒ 預開閘：送 seed＋pre=1 等橋的 gate");
    }

    /** 橋的預開閘判定（POST 回應的 `gate` 或 SSE 的 `ears{kind:gate}`，只認先到的；LOCK 內叫）。 */
    private static void onGate(String gate, boolean ack, String why) {
        if (gateHandled || !TURN.preopen) return;
        gateHandled = true;
        long now = now();
        if ("open".equalsIgnoreCase(gate)) {
            TURN.gateOpen(ack, now);
            if (ack) Native.MAIN.post(BgEars::playAck);   // 只有名字 ⇒ 應答一聲（原 app `onGateDecision` → `playAck`）
            Log.i(TAG, "熄屏耳朵：橋開門（" + (ack ? "只有名字" : "名字之後還有話") + "／" + why + "）⇒ 會話窗 "
                + BgTurn.CONVERSATION_MS / 1000 + " s");
        } else {
            int blocks = TURN.preopenBlocks;
            TURN.gateReject();
            Log.i(TAG, "熄屏耳朵：橋拒絕預開閘（" + why + "）⇒ 丟棄（已上傳 " + blocks + " 塊）、回到等名字");
        }
    }

    // ────────────────────────────────────────────────────────── 上行（自己一條，節拍＝`EarsLink.Update`）

    private static void uplink(int gen) {
        int sent = 0, failures = 0, sentAtProbe = 0;
        long probeAt = now();
        while (running && gen == generation) {
            try { Thread.sleep(30); } catch (InterruptedException ignored) {}
            long now = now();
            if (now - probeAt >= PROBE_MS) {
                BgWake w = net;
                float max = w != null ? w.maxScoreSinceRead : Float.NaN;
                if (w != null) w.maxScoreSinceRead = Float.NaN;
                Log.i(TAG, "熄屏耳朵：探針 30 s：上傳 " + (sent - sentAtProbe) + " 塊"
                    + (useGate && w != null ? "／喚醒網最高分 " + BgWake.f3(max) + "（門檻 " + BgWake.f3(w.kwsThreshold) + "）／"
                        + w.stats() : "／一直送"));
                sentAtProbe = sent;
                probeAt = now;
            }
            byte[] body;
            boolean seed, pre;
            synchronized (LOCK) {
                // ① 閥門（她在講／聾窗）：不送；她講話的時候會話窗跟著延（`conversationFromPlayback`）。
                boolean shut = speaking || SystemClock.elapsedRealtime() < valveUntil;
                if (shut) {
                    if (!valveShut) Log.i(TAG, "熄屏耳朵：閥門關（她說話）");
                    valveShut = true;
                    TURN.conversationFromPlayback(now, 0L);
                    continue;
                }
                if (valveShut) {
                    valveShut = false;
                    lastSentAt = now;                       // 下一塊等滿 0.5 s 閥門之後的聲音
                    cursor = total;
                    Log.i(TAG, TURN.reopenAfterSpeech(now) ? "熄屏耳朵：閥門開 ⇒ 會話窗接著開（不必再喊名字）"
                        : "熄屏耳朵：閥門開");
                }
                // ② 回合沒開：一直送那條就再開一輪；呼びかけ＝等名字（一塊都不送）。
                if (!TURN.armed) {
                    if (useGate) {
                        cursor = total;
                        continue;
                    }
                    TURN.arm(now);
                    cursor = total;
                    lastSentAt = now - BgTurn.STREAM_BLOCK_MS;
                }
                if (thinking) TURN.holdOpen(now, THINK_HOLD_MS);
                boolean wasPre = TURN.preopen;
                if (!TURN.tick(now)) {
                    if (useGate) Log.i(TAG, wasPre ? "熄屏耳朵：預開閘逾時（橋沒回 gate）⇒ 回到等名字"
                        : "熄屏耳朵：會話窗收工 ⇒ 回到等名字");
                    continue;
                }
                if (!TURN.canSend() || now - lastSentAt < BgTurn.STREAM_BLOCK_MS) continue;
                seed = seedPending;
                pre = TURN.preopen;
                short[] take;
                if (seed) {
                    take = recent(RING);
                    if (take.length < BLOCK) continue;      // 環形還沒滿半秒（剛開麥）⇒ 先聽一下
                    seedPending = false;
                    cursor = total;
                } else {
                    long since = Math.min(total - cursor, ringFilled);
                    cursor = total;
                    if (since <= 0) continue;
                    take = recent((int) since);
                }
                if (!TURN.takeBlock()) continue;
                lastSentAt = now;
                blockPre = pre;
                body = pcm16(take);
            }
            Object[] res = null;
            String error = null;
            try {
                res = Http.postBytes("/v1/ears/stream" + query(seed, pre), body);
                int code = (Integer) res[0];
                if (code / 100 != 2) error = "HTTP " + code + " " + res[1];
            } catch (Exception e) {
                error = e.getMessage();
            }
            if (!running || gen != generation) break;
            if (error != null) {
                failures++;
                synchronized (LOCK) { TURN.onStreamFailed(); }
                Log.i(TAG, "熄屏耳朵：上行失敗 " + error);
                try { Thread.sleep(Math.min(5000, 500L * failures)); } catch (InterruptedException ignored) {}
                continue;
            }
            failures = 0;
            sent++;
            String text = (String) res[1];
            if (sent <= 3 || sent % 20 == 0 || seed) {
                Log.i(TAG, "熄屏耳朵：第 " + sent + " 塊" + (seed ? "（seed 環形）" : "") + (pre ? "（pre）" : "")
                    + " ⇒ 橋（" + body.length / 2 + " 樣本）" + (text.length() > 120 ? text.substring(0, 120) : text));
            }
            onUploadDone(text);
        }
        Log.i(TAG, "熄屏耳朵：上行收工（送過 " + sent + " 塊）");
    }

    /** 一塊的回應（`EarsLink.OnUploadDone`）：gate／preopen／sentences／delivered 交給回合節奏。 */
    private static void onUploadDone(String text) {
        JSONObject o;
        try {
            o = new JSONObject(text == null || text.isEmpty() ? "{}" : text);
        } catch (Exception e) {
            synchronized (LOCK) { TURN.onStreamFailed(); }
            Log.i(TAG, "熄屏耳朵：回應解不開 " + e.getMessage());
            return;
        }
        synchronized (LOCK) {
            long now = now();
            JSONObject gate = o.optJSONObject("gate");
            boolean preopen = o.optBoolean("preopen", false);
            if (gate != null && !gate.optString("gate", "").isEmpty()) {
                onGate(gate.optString("gate", ""), gate.optBoolean("ack", false), gate.optString("why", ""));
            } else if (blockPre && TURN.preopen && !preopen) {
                // 這台橋不認預開閘（舊版／這個角色沒權重）⇒ 本機已經叫到名字了，當它開門（真相 `gate_legacy`）。
                preopenSupported = Boolean.FALSE;
                if (!gateHandled) {
                    gateHandled = true;
                    TURN.gateOpen(true, now);
                    Log.i(TAG, "熄屏耳朵：橋不認預開閘 ⇒ 本機喚醒當開門（舊行為）");
                }
            } else if (blockPre && preopen && preopenSupported != Boolean.TRUE) {
                preopenSupported = Boolean.TRUE;
                Log.i(TAG, "熄屏耳朵：橋認預開閘（等它的 gate）");
            }
            JSONArray rows = o.optJSONArray("sentences");
            int count = rows == null ? 0 : rows.length();
            double score = Double.NaN;
            boolean his = false;
            String verdict = "", said = "";
            for (int i = 0; i < count; i++) {
                JSONObject row = rows.optJSONObject(i);
                if (row == null) continue;
                double sc = row.optDouble("score", Double.NaN);
                if (!Double.isNaN(sc)) score = sc;
                verdict = row.optString("verdict", "");
                if ("his".equals(verdict)) his = true;
                if (!row.isNull("text") && !row.optString("text", "").isEmpty()) said = row.optString("text", "");
            }
            boolean delivered = o.optBoolean("delivered", false);
            TURN.onReply(count, delivered, score, now, his);
            if (count > 0) {
                Log.i(TAG, "熄屏耳朵：橋切出 " + count + " 句（verdict=" + verdict + "／delivered=" + (delivered ? 1 : 0) + "）"
                    + (said.isEmpty() ? "" : "：「" + said + "」"));
            }
        }
    }

    private static byte[] pcm16(short[] pcm) {
        byte[] out = new byte[pcm.length * 2];
        for (int i = 0; i < pcm.length; i++) {
            out[i * 2] = (byte) (pcm[i] & 0xFF);
            out[i * 2 + 1] = (byte) ((pcm[i] >> 8) & 0xFF);
        }
        return out;
    }

    /** 帶通（共用層 `VoiceBand` 的移植，與前景 `MicCapture` 同一組係數：兩顆 biquad 串、Q＝1/√2）。 */
    private static final class VoiceBand {
        private static final double SCALE = 32768.0;
        private final boolean enabled;
        private final double[] hi, lo;                      // b0 b1 b2 a1 a2
        private final double[] hs = new double[4], ls = new double[4];   // x1 x2 y1 y2

        VoiceBand(int rate, double highHz, double lowHz, boolean on) {
            enabled = on;
            hi = biquad(rate, clamp(highHz, 20.0, rate * 0.45), true);
            lo = biquad(rate, clamp(lowHz, 200.0, rate * 0.45), false);
        }

        void process(short[] pcm) {
            if (!enabled) return;
            for (int i = 0; i < pcm.length; i++) {
                double y = step(lo, ls, step(hi, hs, pcm[i] / SCALE));
                pcm[i] = (short) (int) clamp(y * SCALE, -SCALE, SCALE - 1);
            }
        }

        private static double step(double[] c, double[] s, double x) {
            double y = c[0] * x + c[1] * s[0] + c[2] * s[1] - c[3] * s[2] - c[4] * s[3];
            s[1] = s[0]; s[0] = x;
            s[3] = s[2]; s[2] = y;
            return y;
        }

        private static double[] biquad(int rate, double f0, boolean high) {
            double w = 2.0 * Math.PI * f0 / rate;
            double alpha = Math.sin(w) / (2.0 * (1.0 / Math.sqrt(2.0)));
            double a0 = 1.0 + alpha, c = Math.cos(w);
            double b0 = (high ? (1.0 + c) : (1.0 - c)) / 2.0 / a0;
            double b1 = (high ? -(1.0 + c) : (1.0 - c)) / a0;
            return new double[] {b0, b1, b0, -2.0 * c / a0, (1.0 - alpha) / a0};
        }

        private static double clamp(double v, double min, double max) {
            return v < min ? min : (v > max ? max : v);
        }
    }

    // ────────────────────────────────────────────────────────── 訂閱

    private static void subscribe(int gen) {
        while (running && gen == generation) {
            HttpURLConnection c = null;
            try {
                // token 只走 `Http.open` 設好的 `Authorization` 標頭（外部評審 P1-2：查詢字串會進橋的存取日誌）
                c = Http.open("/v1/ears/live?session=" + Uri.encode(session), "GET");
                c.setReadTimeout(0);
                c.setRequestProperty("Accept", "text/event-stream");
                live = c;
                if (c.getResponseCode() / 100 != 2) throw new IllegalStateException("HTTP " + c.getResponseCode());
                Log.i(TAG, "熄屏耳朵：訂閱 /v1/ears/live");
                BufferedReader in = new BufferedReader(new InputStreamReader(c.getInputStream(), StandardCharsets.UTF_8));
                String event = "message";
                StringBuilder data = new StringBuilder();
                String line;
                while (running && gen == generation && (line = in.readLine()) != null) {
                    if (line.isEmpty()) {
                        if (data.length() > 0) dispatch(event, data.toString());
                        event = "message";
                        data.setLength(0);
                    } else if (line.startsWith(":")) {
                        // 心跳
                    } else if (line.startsWith("event:")) {
                        event = line.substring(6).trim();
                    } else if (line.startsWith("data:")) {
                        if (data.length() > 0) data.append('\n');
                        data.append(line.startsWith("data: ") ? line.substring(6) : line.substring(5));
                    }
                }
            } catch (Exception e) {
                if (running && gen == generation) Log.i(TAG, "熄屏耳朵：訂閱斷了 " + e.getMessage());
            } finally {
                if (c != null) c.disconnect();
            }
            if (running && gen == generation) {
                try { Thread.sleep(3000); } catch (InterruptedException ignored) {}
            }
        }
    }

    private static void dispatch(String event, String data) {
        String forUnity = data;
        if ("ears".equals(event)) {
            // 橋側的耳朵事件：`heard`＝他在講（會話窗續期）、`gate`＝預開閘的判定（與 POST 回應只認先到的）。
            try {
                JSONObject o = new JSONObject(data);
                String kind = o.optString("kind", "");
                synchronized (LOCK) {
                    if ("heard".equals(kind)) TURN.peerHeard(now());
                    else if ("gate".equals(kind)) onGate(o.optString("gate", ""), o.optBoolean("ack", false), o.optString("why", ""));
                }
            } catch (Exception ignored) {}
        } else if ("started".equals(event)) {
            synchronized (LOCK) { thinking = true; }         // 她在想 ⇒ 會話窗不收（`holdOpen`）
        } else if ("delta".equals(event) || "speech".equals(event) || "done".equals(event) || "error".equals(event)) {
            synchronized (LOCK) { thinking = false; }
        }
        if ("speech".equals(event)) {
            try {
                JSONObject o = new JSONObject(data);
                String audio = o.optString("audio", "");
                if (!audio.isEmpty()) enqueue(audio);
                o.put("audio", "");               // 亮屏補紀錄時不再念一次
                forUnity = o.toString();
            } catch (Exception e) {
                Log.i(TAG, "熄屏耳朵：speech 幀解不開 " + e.getMessage());
            }
        }
        Log.i(TAG, "熄屏耳朵：幀 " + event + (forUnity.length() > 80 ? " " + forUnity.substring(0, 80) : " " + forUnity));
        JSONObject e = Native.event("earsFrame");
        try {
            e.put("event", event);
            e.put("data", forUnity);
        } catch (Exception ignored) {}
        Native.emit(e);
    }

    // ────────────────────────────────────────────────────────── 播放（主執行緒）

    private static void enqueue(String base64) {
        Context ctx = Native.app;
        if (ctx == null) return;
        try {
            // ⚠️ 檔名**不重用**（外部評審 P1-1，2026-10-18）：先前 `clipSeq % 8` ⇒ 回覆超過 8 句時，排在第 9 句的
            //    會蓋掉還沒播的那一句（播錯／同一句播兩遍）。改單調序號，播完（或出錯、或被清掉）就刪。
            File f = new File(ctx.getCacheDir(), "bg-speech-" + (clipSeq++) + ".wav");
            try (FileOutputStream out = new FileOutputStream(f)) {
                out.write(Base64.decode(base64, Base64.DEFAULT));
            }
            speaking = true;
            Native.MAIN.post(() -> {
                QUEUE.add(f);
                if (player == null) playNext();
            });
        } catch (Exception e) {
            Log.i(TAG, "熄屏耳朵：語音寫不出來 " + e.getMessage());
        }
    }

    /**
     * 叫到她、橋說「只有名字」⇒ 播一個短應答（原 app `ShellController.playAck`：熄屏時它的殼照樣響；
     * Unity 這邊熄屏時主迴圈停了，所以在這裡播）。**獨立的** MediaPlayer，不碰 {@link #player}
     * （她的語音、插話與打斷都掛在那一個上）；`prepareAsync`（C2：別在主執行緒上解碼）。
     */
    private static void playAck() {
        String dir = ackDir;
        if (dir.isEmpty() || !running) return;
        File[] files = new File(dir).listFiles((d, name) -> name.endsWith(".wav"));
        if (files == null || files.length == 0) return;
        File pick = files[new java.util.Random().nextInt(files.length)];
        try {
            final MediaPlayer mp = new MediaPlayer();
            mp.setDataSource(pick.getAbsolutePath());
            mp.setVolume(volume, volume);
            mp.setOnCompletionListener(done -> { done.release(); if (ackPlayer == done) ackPlayer = null; });
            mp.setOnErrorListener((bad, what, extra) -> { bad.release(); if (ackPlayer == bad) ackPlayer = null; return true; });
            mp.setOnPreparedListener(MediaPlayer::start);
            MediaPlayer old = ackPlayer;
            if (old != null) { try { old.release(); } catch (Exception ignored) {} }
            ackPlayer = mp;
            mp.prepareAsync();
            Log.i(TAG, "熄屏耳朵：應答音 " + pick.getName());
        } catch (Exception e) {
            Log.i(TAG, "熄屏耳朵：應答音播不了 " + e.getMessage());
        }
    }

    private static void playNext() {
        File next = QUEUE.poll();
        // ⚠️ `running` では止めない（2026-10-19）：Unity から引き継いだ句（{@link #handOver}）は耳が無くても鳴らす。
        //    前景に戻ったときは {@link #park} が止めて殘りを Unity に返す（以前は亮屏で捨てていた）。
        if (next == null) {
            speaking = false;
            // 聾窗（`deafWindowMs`）：她的尾音還在空氣裡；AEC 真的掛上 ⇒ 不等。
            MicFx f = fx;
            // 聾窗用**單調時鐘**（外部評審 P2-2）：牆鐘被 NTP／改時間動一下，聾窗就變成永遠開或永遠關。
            valveUntil = SystemClock.elapsedRealtime()
                + MicFx.deafWindowMs(MicFx.bargeIn, f != null && f.echoCancelled);
            return;
        }
        try {
            final MediaPlayer p = new MediaPlayer();
            p.setDataSource(next.getAbsolutePath());
            p.setVolume(volume, volume);
            // ⚠️ 外部評審 v2 C2：`prepare()` 是**同步**的（讀檔＋解碼頭），而這一支是在主執行緒上跑
            //    （`Native.MAIN.post`）⇒ 播放前那一下會卡住 `Native.poll` 與事件佇列（亮屏後補紀錄、
            //    收 `earsFrame` 都跟著慢）。改 `prepareAsync()`，真的起播放到 `onPrepared`（回呼也在主執行緒）。
            p.setOnCompletionListener(mp -> { mp.release(); if (player == mp) player = null; next.delete(); playNext(); });
            p.setOnErrorListener((mp, what, extra) -> {
                mp.release(); if (player == mp) player = null; next.delete(); playNext(); return true;
            });
            p.setOnPreparedListener(mp -> {
                // 準備好之前被換掉（{@link #park}）⇒ 這一檔不播（もう Unity 側に返してある）。
                if (mp != player) {
                    // 被換掉 ⇒ 隊列已歸新的那條鏈（或已清空）管，這裡**不再** playNext（否則兩條鏈同時播）。
                    final boolean ours = player == mp;
                    if (ours) player = null;
                    try { mp.release(); } catch (Exception ignored) {}
                    next.delete();
                    if (ours) playNext();
                    return;
                }
                mp.setVolume(volume, volume);
                if (next == seekFile && seekMs > 0) {
                    try { mp.seekTo(seekMs); } catch (Exception ignored) {}
                }
                if (next == seekFile) seekFile = null;
                try {
                    mp.start();
                } catch (Exception e) {
                    Log.i(TAG, "熄屏耳朵：起播失敗 " + e.getMessage());
                    if (player == mp) player = null;
                    mp.release();
                    next.delete();
                    playNext();
                }
            });
            player = p;
            playing = next;
            p.prepareAsync();
        } catch (Exception e) {
            Log.i(TAG, "熄屏耳朵：播不了 " + e.getMessage());
            player = null;
            next.delete();
            playNext();
        }
    }

    // ────────────────────────────────────────────────────────── Unity ↔ 原生の受け渡し（主執行緒）
    //
    // owner 2026-10-19：「从应用返回桌面后，播放中的音频会被暂停，而不会后台播放」。Unity の AudioSource は
    // activity が止まると一緒に止まる（主迴圈ごと）。原 app は原生の MediaPlayer なので背景でも鳴り續ける。
    // ⇒ Unity が背景に回る瞬間（`SpeechQueue.OnApplicationPause`）に「鳴っていた一句の殘り＋待ち行列」を
    //    ここへ渡して鳴らし續け（{@link #handOver}）、前景に戻ったら殘りを Unity に返す（{@link #park}／{@link #takeBack}）。

    private static File seekFile;            // 引き継いだ先頭の一句：ここから（ms）
    private static int seekMs;
    private static final java.util.ArrayList<File> PARKED = new java.util.ArrayList<>();
    private static int parkedPos;

    static void handOver(final java.util.List<File> files, final int startMs) {
        Native.MAIN.post(() -> {
            if (files.isEmpty()) return;
            ArrayDeque<File> rest = new ArrayDeque<>(QUEUE);
            QUEUE.clear();
            QUEUE.addAll(files);
            QUEUE.addAll(rest);
            seekFile = files.get(0);
            seekMs = Math.max(0, startMs);
            speaking = true;
            Log.i(TAG, "背景再生：Unity から " + files.size() + " 句を引き継ぐ（先頭は " + seekMs + " ms から）");
            if (player == null) playNext();
        });
    }

    /** 背景中に屆いた回覆の一句（{@link Native#appendSpeech}）：隊列の後ろへ（引き継いだ句より後に鳴る）。 */
    static void append(final File f) {
        Native.MAIN.post(() -> {
            QUEUE.add(f);
            speaking = true;
            if (player == null) playNext();
        });
    }

    /** 前景に戻った：鳴らしていた一句（位置つき）と待ち行列を止めて取っておく（消さない；C# が取りに來る）。 */
    private static void park() {
        MediaPlayer p = player;
        File cur = playing;
        int pos = 0;
        if (p != null) { try { pos = p.getCurrentPosition(); } catch (Exception ignored) {} }
        if (cur != null && cur == seekFile) pos += seekMs;            // まだ準備中（seek 前）なら引き継いだ位置のまま
        for (File f : PARKED) f.delete();                              // 前回だれも取りに來なかった殘り
        PARKED.clear();
        if (cur != null && cur.isFile()) PARKED.add(cur);
        PARKED.addAll(QUEUE);
        QUEUE.clear();
        parkedPos = cur != null ? pos : 0;
        player = null;
        playing = null;
        seekFile = null;
        if (p != null) {
            try { p.stop(); } catch (Exception ignored) {}
            p.release();
        }
        speaking = false;
        if (!PARKED.isEmpty()) {
            Log.i(TAG, "背景再生：前景に戻る ⇒ 殘り " + PARKED.size() + " 句を Unity に返す（先頭 " + parkedPos + " ms から）");
        }
    }

    /** C#（Unity のスレッド）から：{@link #park} で取っておいた分 `{"pos":ms,"files":[...]}`（取ったら空になる）。 */
    static String takeBack() {
        final String[] out = {"{\"pos\":0,\"files\":[]}"};
        final java.util.concurrent.CountDownLatch done = new java.util.concurrent.CountDownLatch(1);
        Native.MAIN.post(() -> {
            try {
                JSONArray a = new JSONArray();
                for (File f : PARKED) a.put(f.getAbsolutePath());
                JSONObject o = new JSONObject();
                o.put("pos", parkedPos);
                o.put("files", a);
                out[0] = o.toString();
                PARKED.clear();
                parkedPos = 0;
            } catch (Exception ignored) {}
            done.countDown();
        });
        try { done.await(1, java.util.concurrent.TimeUnit.SECONDS); } catch (InterruptedException ignored) {}
        return out[0];
    }
}
