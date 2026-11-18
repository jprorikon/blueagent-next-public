package dev.deepsea.blueagent.shell;

import android.os.SystemClock;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * **熄屏耳朵的喚醒網＋①聲紋閘**：共用層 `WakeKws.kt`（＋`GateWindow.kt`）狀態機的 Java 逐段移植。
 *
 * 為什麼又寫一份：前景那條是 C# 的 `WakeKws`（`WakeNet.cs` 在它自己的執行緒上餵），但 Unity 一 onStop
 * 主迴圈就停，熄屏那段的收音在 {@link BgEars}（Java）——它要自己判「叫到她了沒」，不然只能 24/7 上傳
 * （2026-10-18 14:27–14:34 電視開著：~1800 塊電視聲上了橋、橋拒了 49 句）。
 *
 * **只移植狀態機**（門檻＋連續幀＋冷卻、閘門的延時／閥門／tail|best 窗、引擎與門檻成對的 SpkGate）；
 * 算術全在原生：`libblueagent_wake.so`（與前景同一顆，`wake_c_api.h`）與 `libsherpa-onnx-c-api.so`，
 * 經 `libblueagent_bgwake.so` 這一層 JNI（`unity/native/bgwake/bgwake_jni.c`）。
 *
 * 執行緒：{@link #onBlock} 與引擎的 push／recent／reset／build 都在**麥克風執行緒**（{@link BgEars} 的那一條）；
 * 打分（sherpa 68–371 ms）丟 {@link #GATE}（一條、序列）。叫醒／拒絕的回呼落在 {@link #GATE} 或麥克風執行緒。
 *
 * 跟真相的差：`windowDir`（存閘門窗）沒移植（診斷用）；金標自檢不重跑——前景那顆網（同一份模型檔）的自檢
 * 結果由 C# 經 {@link Config#usable} 交過來。
 */
final class BgWake {
    private static final String TAG = Native.TAG;

    static final int BLOCK = 480;                      // 30 ms（`MicListener.BLOCK_SIZE`）
    static final int SAMPLE_RATE = 16000;
    // ── GateWindow ─────────────────────────────────────────────────────────────────────────────
    static final int WINDOW_SAMPLES = 24000;           // 1.5 s：聲紋網訓練／`Spk::cosine` 的窗
    static final int SPAN_SAMPLES = 48000;             // 3 s：環形要留的跨距（best 才有地方看）
    static final int STEP_SAMPLES = 4000;              // 0.25 s
    static final String RULE_TAIL = "tail";
    static final String RULE_BEST = "best";
    private static final float BLOB_BAR = 0.689f;      // `WakeNet.BlobBar`（學生 blob 的操作點）

    /** 閘門打分那一條（`AndroidWakeDispatch`：一個行程一條、序列執行）。 */
    static final ExecutorService GATE = Executors.newSingleThreadExecutor(r -> new Thread(r, "ba-bg-spk-gate"));

    private static volatile boolean loaded;
    private static volatile String loadError = "";

    static boolean loadNative() {
        if (loaded) return true;
        try {
            System.loadLibrary("blueagent_bgwake");
            loaded = true;
        } catch (Throwable t) {
            loadError = t.getClass().getSimpleName() + ": " + t.getMessage();
        }
        return loaded;
    }

    // ── JNI（`bgwake_jni.c`；名字要與這個類別對得上）──────────────────────────────────────────
    static native long kwsCreate(byte[] blob, String[] error);
    static native float kwsThreshold(long h);
    static native void kwsDecideEvery(long h, int every);
    static native void kwsPcmCap(long h, int samples);
    static native float[] kwsPush(long h, short[] pcm, int n);
    static native float[] kwsRecent(long h, int samples);
    static native void kwsReset(long h);
    static native void kwsDestroy(long h);
    static native long spkCreate(byte[] blob, String[] error);
    static native float spkThreshold(long h);
    static native float spkCosine(long h, float[] window);
    static native void spkDestroy(long h);
    static native long sherpaCreate(String model, int threads, String provider);
    static native int sherpaDim(long h);
    static native float[] sherpaEmbed(long h, float[] window, int rate);
    static native void sherpaDestroy(long h);

    /** C# `BridgeShell.SyncEars` 交過來的那一份（`Native.setWake`）。 */
    static final class Config {
        boolean on;                 // 呼びかけ＋kws_enabled＋不是閘常開的模式
        boolean usable;             // 前景那顆網起得來、金標自檢沒失敗
        String modelsDir = "";
        String character = "";
        float kwsThreshold;         // 0＝blob 自帶的操作點
        int consecutive = 3;        // 基準 3（owner 2026-10-18；同 C# `SettingsStore.KwsConsecutive`）
        int decideEvery = 1;
        long cooldownMs = 1500;
        boolean gateOn;
        float speakerThreshold;     // 學生 blob 的門檻覆蓋（0＝blob 自帶）
        String gateRule = RULE_TAIL;
        long minEvalMs = 400;
        long gateDelayMs = 400;
        String spkSherpa = "";      // 質心檔名（空＝用學生 blob）
        float spkSherpaThreshold;   // 他自己填的 sherpa 門檻（0＝質心檔烤好的）
        String sherpaProvider = "cpu";
        int sherpaThreads = 2;

        /** 會讓網要重建的那幾格（`WakeNet.Ensure` 的 signature）。 */
        String signature() {
            return String.join("|", modelsDir, character, String.valueOf(kwsThreshold), String.valueOf(consecutive),
                String.valueOf(decideEvery), String.valueOf(cooldownMs), String.valueOf(gateOn),
                String.valueOf(speakerThreshold), gateRule, String.valueOf(minEvalMs), String.valueOf(gateDelayMs),
                spkSherpa, String.valueOf(spkSherpaThreshold), sherpaProvider, String.valueOf(sherpaThreads));
        }
    }

    interface Listener {
        /** 叫醒：KWS 分數、觸發時刻（elapsedRealtime）、閘門的 cos（NaN＝閘關著／沒窗／打分失敗）。 */
        void onWake(float kws, long at, float cos);

        /** 閘門判「不是他」。 */
        void onReject(float cos, float bar);
    }

    /** 一顆 sherpa 引擎＋它自己那把尺（`WakeKws.SpkGate`：引擎與門檻成對換）。 */
    private static final class SherpaGate {
        final long handle;
        final float[] centroid;
        final float threshold;
        final String label;

        SherpaGate(long handle, float[] centroid, float threshold, String label) {
            this.handle = handle;
            this.centroid = centroid;
            this.threshold = threshold;
            this.label = label;
        }
    }

    // ── 狀態（`WakeKws` 的欄位）────────────────────────────────────────────────────────────────
    private final Listener listener;
    private final String signature;
    final String character;
    float kwsThreshold;
    private final int consecutive;
    private final long cooldownMs;
    private final boolean gateOn;
    private final float speakerThreshold;
    private final String gateRule;
    private final long minEvalMs;
    private final long gateDelayMs;
    private volatile SherpaGate sherpaGate;
    private volatile boolean closed;

    private long kws;
    private volatile long spk;
    private float spkBlobThreshold;
    private int hits;
    private long lastTrigger;
    private long blocks, triggers;
    private volatile long wakes, rejects, skips;
    private long lastGateAt;
    private float pendingS = Float.NaN;
    private long pendingAt;
    float maxScoreSinceRead = Float.NaN;

    private BgWake(Config c, Listener listener) {
        this.listener = listener;
        this.signature = c.signature();
        this.character = c.character;
        this.kwsThreshold = c.kwsThreshold;
        this.consecutive = Math.max(1, c.consecutive);
        this.cooldownMs = c.cooldownMs;
        this.gateOn = c.gateOn;
        this.speakerThreshold = c.speakerThreshold;
        this.gateRule = c.gateRule == null ? RULE_TAIL : c.gateRule;
        this.minEvalMs = c.minEvalMs;
        this.gateDelayMs = c.gateDelayMs;
    }

    boolean matches(Config c) {
        return !closed && signature.equals(c.signature());
    }

    private static long clock() {
        return SystemClock.elapsedRealtime();
    }

    /** 引擎與門檻成對（`gateBar`）：掛了 sherpa ⇒ 它的尺；否則學生 blob（覆蓋 > blob 自帶）。 */
    float gateBar() {
        SherpaGate g = sherpaGate;
        if (g != null) return g.threshold;
        return speakerThreshold > 0f ? speakerThreshold : spkBlobThreshold;
    }

    String gateLabel() {
        SherpaGate g = sherpaGate;
        return g != null ? g.label : "blob（學生）";
    }

    String stats() {
        return "區塊 " + blocks + "、觸發 " + triggers + "、放行 " + wakes + "、判非本人 " + rejects
            + (skips > 0 ? "、略過 " + skips : "");
    }

    /**
     * 照設定建一顆網（`WakeKws.start` ＋ `WakeNet.Build`），**麥克風執行緒上叫**。失敗回 null 並在 why[0] 寫原因。
     * sherpa 那顆在 {@link #GATE} 上背景載（0.2–0.6 s），到了才換手；之前閘門是學生 blob。
     */
    static BgWake build(Config c, Listener listener, String[] why) {
        if (!loadNative()) {
            why[0] = "JNI 載不起來：" + loadError;
            return null;
        }
        File kwsFile = new File(new File(c.modelsDir, c.character), "kws_phone_fp32.bin");
        File spkFile = new File(c.modelsDir, "spk_phone_fp32.bin");
        byte[] kwsBytes = read(kwsFile);
        if (kwsBytes == null) {
            why[0] = "讀不到 " + kwsFile;
            return null;
        }
        String[] err = {""};
        long k = kwsCreate(kwsBytes, err);
        if (k == 0) {
            why[0] = "喚醒模型壞了：" + err[0];
            return null;
        }
        BgWake w = new BgWake(c, listener);
        w.kws = k;
        if (w.kwsThreshold <= 0f) w.kwsThreshold = kwsThreshold(k);
        kwsDecideEvery(k, Math.max(1, c.decideEvery));
        // 環形要裝得下閘門規則看得最寬的那一段，不然前面補零＝每一扇窗三分之一是數位靜音（`GateWindow` 檔頭）。
        kwsPcmCap(k, SPAN_SAMPLES);
        if (w.gateOn) {
            byte[] spkBytes = read(spkFile);
            long s = spkBytes == null ? 0 : spkCreate(spkBytes, err);
            if (s == 0) {
                kwsDestroy(k);
                why[0] = spkBytes == null ? "讀不到 " + spkFile : "聲紋模型壞了：" + err[0];
                return null;
            }
            w.spk = s;
            w.spkBlobThreshold = spkThreshold(s);
            if (c.spkSherpa != null && !c.spkSherpa.trim().isEmpty()) w.attachSherpa(c);
        }
        Log.i(TAG, "熄屏喚醒網：起來了（" + c.character + "：門檻 " + f3(w.kwsThreshold) + "、連續 " + w.consecutive
            + "、每 " + Math.max(1, c.decideEvery) + " 幀一決、冷卻 " + w.cooldownMs + "ms；閘門"
            + (w.gateOn ? "開：" + w.gateLabel() + " 門檻 " + f3(w.gateBar()) + "、" + w.gateRule
                + (w.gateDelayMs > 0 ? "、延時 " + w.gateDelayMs + "ms" : "") : "關") + "）");
        return w;
    }

    /** `attachSherpaEngines`／`loadSherpaEngine`：質心檔指名模型；在 {@link #GATE} 上載，到了才換手。 */
    private void attachSherpa(Config c) {
        final String name = c.spkSherpa.trim();
        final File centroidFile = new File(c.modelsDir, name);
        final float student = c.speakerThreshold > 0f ? c.speakerThreshold : BLOB_BAR;
        GATE.execute(() -> {
            String note;
            try {
                byte[] raw = read(centroidFile);
                if (raw == null) {
                    note = "找不到 " + name + "（要 adb push 到 " + c.modelsDir + "）⇒ 用內建學生";
                } else {
                    JSONObject o = new JSONObject(new String(raw, StandardCharsets.UTF_8));
                    String model = o.optString("model", "");
                    JSONArray arr = o.optJSONArray("centroid");
                    double baked = o.optDouble("threshold", Double.NaN);
                    // `SpeakerBars.Effective`：他自己填的 > 質心檔烤好的 > 學生 blob 的尺。
                    float bar = c.spkSherpaThreshold > 0f ? c.spkSherpaThreshold
                        : !Double.isNaN(baked) ? (float) baked : student;
                    File modelFile = new File(c.modelsDir, model);
                    if (model.isEmpty()) {
                        note = name + " 沒有 model 欄位 ⇒ 用內建學生";
                    } else if (!modelFile.exists()) {
                        note = "找不到模型 " + model + " ⇒ 用內建學生";
                    } else {
                        long h = sherpaCreate(modelFile.getAbsolutePath(), c.sherpaThreads, c.sherpaProvider);
                        int dim = h == 0 ? 0 : sherpaDim(h);
                        float[] centroid = new float[arr == null ? 0 : arr.length()];
                        for (int i = 0; i < centroid.length; i++) centroid[i] = (float) arr.optDouble(i, 0.0);
                        if (h == 0) {
                            note = "sherpa 建不起來：" + model + " ⇒ 用內建學生";
                        } else if (centroid.length == 0 || centroid.length != dim) {
                            sherpaDestroy(h);
                            note = "質心維度 " + centroid.length + " 與模型 " + dim + " 不符 ⇒ 用內建學生";
                        } else if (closed) {
                            sherpaDestroy(h);
                            note = "載入期間網被換掉：這顆引擎丟棄";
                        } else {
                            String label = name.replace("spk_centroid_", "").replace(".json", "");
                            sherpaGate = new SherpaGate(h, centroid, bar, label);
                            note = "閘門就緒：" + label + "（dim " + dim + "、" + c.sherpaProvider + "、門檻 " + f3(bar) + "）";
                        }
                    }
                }
            } catch (Throwable t) {
                note = "sherpa 載入例外：" + t.getMessage() + " ⇒ 用內建學生";
            }
            Log.i(TAG, "熄屏喚醒網：" + note);
        });
    }

    /** 麥克風重開：舊音訊不算進新的一輪（`reset`）。 */
    void reset() {
        if (kws != 0) kwsReset(kws);
        hits = 0;
        pendingS = Float.NaN;
    }

    /** 收掉（麥克風執行緒上叫）：KWS 當場放；聲紋引擎排到 {@link #GATE} 上放（可能正在打分）。 */
    void close() {
        closed = true;
        if (kws != 0) kwsDestroy(kws);
        kws = 0;
        GATE.execute(() -> {
            long s = spk;
            spk = 0;
            if (s != 0) spkDestroy(s);
            SherpaGate g = sherpaGate;
            sherpaGate = null;
            if (g != null) sherpaDestroy(g.handle);
        });
    }

    /**
     * 30 ms 一塊（麥克風執行緒；`WakeKws.onBlock` 逐段）：算分數、確認連續幀、套冷卻；閘門只**抄窗**，
     * 打分交給 {@link #GATE}。
     */
    void onBlock(short[] samples) {
        long engine = kws;
        if (engine == 0) return;
        blocks++;
        // 擱著的觸發等夠了沒（gateDelayMs）？夠了就在這條執行緒抄窗，打分照舊丟 GATE。
        float pendS = pendingS;
        if (!Float.isNaN(pendS) && clock() - pendingAt >= gateDelayMs) {
            pendingS = Float.NaN;
            judge(engine, pendS, pendingAt);
        }
        float[] scores = kwsPush(engine, samples, samples.length);
        for (float s : scores) {
            if (Float.isNaN(s)) continue;
            if (Float.isNaN(maxScoreSinceRead) || s > maxScoreSinceRead) maxScoreSinceRead = s;
            if (s >= kwsThreshold) {
                hits++;
            } else {
                hits = 0;
                continue;
            }
            if (hits < consecutive) continue;
            hits = 0;
            long now = clock();
            if (now - lastTrigger < cooldownMs) continue;
            lastTrigger = now;
            triggers++;
            if (!gateOn) {
                logLine("wake", now, s, "gate=false");
                wakeThrough(s, now, Float.NaN);
                continue;
            }
            // 閘門閥門：sherpa 打分很貴，太密的觸發不打分、也**不叫醒**。
            if (sherpaGate != null && minEvalMs > 0 && now - lastGateAt < minEvalMs) {
                skips++;
                logLine("wake_skip", now, s, "閘門閥門（" + (now - lastGateAt) + "ms < " + minEvalMs + "）");
                continue;
            }
            lastGateAt = now;
            if (gateDelayMs > 0) {
                // 先擱著：等名字的尾巴進來再判。同一個觸發只擱一次。
                if (Float.isNaN(pendingS)) { pendingS = s; pendingAt = now; }
                continue;
            }
            judge(engine, s, now);
        }
    }

    /** 抄窗（麥克風執行緒）＋打分丟 GATE。空窗＝**放行**（閘門是加在鏈上的檢查，不該變成聾）。 */
    private void judge(long engine, float s, long at) {
        float[] span = kwsRecent(engine, SPAN_SAMPLES);
        if (span.length == 0) {
            logLine("wake_gate", at, s, "沒有可用的窗 ⇒ 放行");
            wakeThrough(s, at, Float.NaN);
            return;
        }
        GATE.execute(() -> runGate(s, at, span));
    }

    /** 打分（GATE 上）。判過＝叫醒；判不過＝`wake_reject`，不叫醒。 */
    private void runGate(float s, long at, float[] span) {
        if (closed) return;
        SherpaGate eng = sherpaGate;
        float bar = gateBar();
        long t0 = clock();
        float best = Float.NaN;
        for (int start : starts(span.length, gateRule)) {
            float cos = score(window(span, start), eng);
            if (Float.isNaN(cos)) continue;
            if (Float.isNaN(best) || cos > best) best = cos;
        }
        long cost = clock() - t0;
        String label = eng != null ? eng.label : "blob";
        if (Float.isNaN(best)) {
            logLine("wake_gate", at, s, "cos=null bar=" + f3(bar) + " rule=" + gateRule + " ms=" + cost + " 打分失敗 ⇒ 放行");
            wakeThrough(s, at, Float.NaN);
            return;
        }
        if (best >= bar) {
            logLine("wake", at, s, "gate=true cos=" + f3(best) + " bar=" + f3(bar) + " rule=" + gateRule
                + " delay=" + gateDelayMs + " ms=" + cost + " " + label);
            wakeThrough(s, at, best);
        } else {
            rejects++;
            logLine("wake_reject", at, s, "cos=" + f3(best) + " bar=" + f3(bar) + " rule=" + gateRule
                + " delay=" + gateDelayMs + " ms=" + cost + " 聲紋拒絕（" + label + "）");
            listener.onReject(best, bar);
        }
    }

    /** 一扇窗的分數：掛了 sherpa 就用它（單位長度 embedding · 質心），否則學生 blob。 */
    private float score(float[] window, SherpaGate eng) {
        if (eng != null) {
            float[] e = sherpaEmbed(eng.handle, window, SAMPLE_RATE);
            if (e == null || e.length != eng.centroid.length) return Float.NaN;
            double sum = 0;
            for (float v : e) sum += (double) v * v;
            float norm = Math.max((float) Math.sqrt(sum), 1e-9f);
            float dot = 0f;
            for (int i = 0; i < e.length; i++) dot += (e[i] / norm) * eng.centroid[i];
            return dot;
        }
        long student = spk;
        return student == 0 ? Float.NaN : spkCosine(student, window);
    }

    private void wakeThrough(float s, long at, float cos) {
        wakes++;
        listener.onWake(s, at, cos);
    }

    // ── GateWindow ─────────────────────────────────────────────────────────────────────────────

    /** 要打分的窗起點（`GateWindow.starts`）：一定至少一個，結束在 span 尾巴那一扇一定在裡面。 */
    static int[] starts(int spanSamples, String rule) {
        int last = Math.max(0, spanSamples - WINDOW_SAMPLES);
        if (!RULE_BEST.equals(rule) || last == 0) return new int[] {last};
        ArrayList<Integer> out = new ArrayList<>(last / STEP_SAMPLES + 2);
        for (int s = 0; s <= last; s += STEP_SAMPLES) out.add(s);
        if (out.get(out.size() - 1) != last) out.add(last);
        int[] r = new int[out.size()];
        for (int i = 0; i < r.length; i++) r[i] = out.get(i);
        return r;
    }

    /** 一扇窗（`GateWindow.window`）：正好 WINDOW_SAMPLES 長；不夠就在**前面**補零。 */
    static float[] window(float[] span, int start) {
        int from = Math.max(0, Math.min(start, span.length));
        int have = Math.max(0, Math.min(span.length - from, WINDOW_SAMPLES));
        float[] out = new float[WINDOW_SAMPLES];
        if (have > 0) System.arraycopy(span, from, out, WINDOW_SAMPLES - have, have);
        return out;
    }

    // ── 雜項 ───────────────────────────────────────────────────────────────────────────────────

    private void logLine(String kind, long at, float s, String rest) {
        Log.i(TAG, "熄屏喚醒網：" + kind + " at=" + at + " s=" + f3(s) + " kws=" + f3(kwsThreshold) + " " + rest
            + " blocks=" + blocks);
    }

    static String f3(float v) {
        return Float.isNaN(v) ? "—" : String.format(Locale.ROOT, "%.3f", v);
    }

    private static byte[] read(File f) {
        try {
            return f.isFile() ? Files.readAllBytes(f.toPath()) : null;
        } catch (Exception e) {
            return null;
        }
    }
}
