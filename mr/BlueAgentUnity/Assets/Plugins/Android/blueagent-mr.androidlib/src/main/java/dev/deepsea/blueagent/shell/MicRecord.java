package dev.deepsea.blueagent.shell;

import android.media.AudioFormat;
import android.media.AudioRecord;
import android.util.Log;

import org.json.JSONObject;

import java.util.List;

/**
 * 〔MR 包的副本：2026-11-01 從手機 androidlib 搬過來，只改了 TAG。兩份要一起改。〕
 *
 * **前景的收音**（C# `MicCapture` 的原生那一半）：`AudioRecord`（音源＝設定 `audio_source`）＋
 * 平台 NS／AGC／AEC（{@link MicFx}，`MicEffects.attach()` 同一套）。
 *
 * 為什麼不用 Unity 的 `Microphone`：它的音源固定、沒有辦法掛 `AcousticEchoCanceler`，
 * 原 app 抵擋「聽到自己」的第一層（可選音源＋平台 AEC）在那條路上根本不存在。
 *
 * 形狀刻意很薄：錄音執行緒把 PCM16 寫進一個 3 秒的先進先出；C# 每幀 {@link #drain} 一次搬走
 * （環形緩衝、seed、電平都照舊在 C#）。C# 卡住超過 3 秒 ⇒ 丟最舊的（不讓它無限長）。
 */
public final class MicRecord {
    // MR 包裡沒有手機殼的 `Native`（JNI 總入口、帶 activity／通知，不進頭顯）⇒ 自己的 tag
    private static final String TAG = "BlueAgentMic";
    private static final int RATE = 16000;
    private static final int CHUNK = RATE / 50;                  // 20 ms 讀一次
    private static final short[] EMPTY = new short[0];

    private static final Object LOCK = new Object();
    private static final short[] FIFO = new short[RATE * 3];
    private static int head, size;                               // FIFO 讀頭／待取樣本數

    private static AudioRecord record;
    private static MicFx fx;
    private static Thread thread;
    private static volatile boolean running;

    /** 開麥中，AEC 真的掛上了（C# 的聾窗用它；`MicListener.echoCancelled`）。 */
    static volatile boolean echoCancelled;

    private MicRecord() {}

    /**
     * 開麥；回 JSON：`{ok, source, available, attached, aec, why}`
     * （`attached`＝真的掛上的效果，`"無"`＝一個都沒有；同原版 `onMicInfo` 的三欄）。
     */
    @SuppressWarnings("MissingPermission")    // C# 先要好 RECORD_AUDIO
    public static synchronized String start(String sourceName, boolean effects) {
        JSONObject o = new JSONObject();
        String src = MicFx.SOURCES.containsKey(sourceName) ? sourceName : "voice_recognition";
        try {
            o.put("source", src);
            if (running) stop();
            MicFx.source = src;
            MicFx.effects = effects;
            int min = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
            AudioRecord r = new AudioRecord(MicFx.sourceId(src), RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT, Math.max(min, CHUNK * 2 * 8));
            if (r.getState() != AudioRecord.STATE_INITIALIZED) {
                try { r.release(); } catch (Exception ignored) {}
                o.put("ok", false);
                o.put("why", "AudioRecord 沒初始化（source=" + src + "）");
                return o.toString();
            }
            r.startRecording();
            MicFx made = new MicFx(r, effects);
            List<String> names = made.attach();
            record = r;
            fx = made;
            echoCancelled = made.echoCancelled;
            synchronized (LOCK) { head = 0; size = 0; }
            running = true;
            final AudioRecord rec = r;
            thread = new Thread(() -> loop(rec), "ba-mic");
            thread.start();
            o.put("ok", true);
            o.put("available", made.describe());
            o.put("attached", MicFx.joined(names));
            o.put("aec", echoCancelled);
            Log.i(TAG, "收音：開 source=" + src + " " + made.describe() + " 掛上=" + MicFx.joined(names));
        } catch (Exception e) {
            try { o.put("ok", false); o.put("why", String.valueOf(e.getMessage())); } catch (Exception ignored) {}
            Log.i(TAG, "收音：開不了 " + e.getMessage());
        }
        return o.toString();
    }

    public static synchronized void stop() {
        if (!running && record == null) return;
        running = false;
        Thread t = thread;
        thread = null;
        AudioRecord r = record;
        record = null;
        if (r != null) { try { r.stop(); } catch (Exception ignored) {} }
        if (t != null) { try { t.join(500); } catch (InterruptedException ignored) {} }
        if (fx != null) fx.release();             // 效果握著 session ⇒ 先放
        fx = null;
        if (r != null) { try { r.release(); } catch (Exception ignored) {} }
        echoCancelled = false;
        synchronized (LOCK) { head = 0; size = 0; }
        Log.i(TAG, "收音：關");
    }

    public static boolean isRunning() {
        return running;
    }

    public static boolean echoCancelled() {
        return echoCancelled;
    }

    /** 上次 drain 之後錄到的全部樣本（沒有＝空陣列）。 */
    public static short[] drain() {
        synchronized (LOCK) {
            if (size == 0) return EMPTY;
            short[] out = new short[size];
            int first = Math.min(size, FIFO.length - head);
            System.arraycopy(FIFO, head, out, 0, first);
            if (first < size) System.arraycopy(FIFO, 0, out, first, size - first);
            head = (head + size) % FIFO.length;
            size = 0;
            return out;
        }
    }

    private static void loop(AudioRecord r) {
        short[] chunk = new short[CHUNK];
        while (running) {
            int n;
            try { n = r.read(chunk, 0, CHUNK); } catch (Exception e) { break; }
            if (n <= 0) {
                if (!running) break;
                continue;
            }
            synchronized (LOCK) {
                for (int i = 0; i < n; i++) {
                    int at = (head + size) % FIFO.length;
                    FIFO[at] = chunk[i];
                    if (size < FIFO.length) size++;
                    else head = (head + 1) % FIFO.length;   // 滿了 ⇒ 丟最舊
                }
            }
        }
    }
}
