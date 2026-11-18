package dev.deepsea.blueagent.shell;

import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.media.audiofx.AcousticEchoCanceler;
import android.media.audiofx.AudioEffect;
import android.media.audiofx.AutomaticGainControl;
import android.media.audiofx.NoiseSuppressor;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 收音路徑＋平台效果——原 app `MicEffects.kt` 的逐行移植（Unity 版前景 {@link MicRecord} 與熄屏 `BgEars` 共用；MR 包沒有 BgEars）。
 *
 * 要點照真相：
 * <ul>
 *   <li>音源是設定（`audio_source`）：{@link #SOURCES} 的順序＝設定頁的順序；不認得的名字＝VOICE_RECOGNITION。</li>
 *   <li>NS／AGC／AEC 能不能掛是**裝置**的事，被拒是**靜默**被拒 ⇒ 只能「試、然後照實回報」：
 *       {@link #attach} 回真的掛上的那幾個；{@link #echoCancelled} 決定她講完之後要不要聾窗
 *       （`deafWindowMs`：AEC 有 ⇒ 0）。</li>
 *   <li>效果握著 audio session ⇒ {@link #release} 要在 `AudioRecord.release()` 之前。</li>
 * </ul>
 */
final class MicFx {
    static final Map<String, Integer> SOURCES = new LinkedHashMap<>();
    static {
        SOURCES.put("voice_recognition", MediaRecorder.AudioSource.VOICE_RECOGNITION);
        SOURCES.put("voice_communication", MediaRecorder.AudioSource.VOICE_COMMUNICATION);
        SOURCES.put("mic", MediaRecorder.AudioSource.MIC);
        SOURCES.put("camcorder", MediaRecorder.AudioSource.CAMCORDER);
        SOURCES.put("unprocessed", MediaRecorder.AudioSource.UNPROCESSED);
        SOURCES.put("default", MediaRecorder.AudioSource.DEFAULT);
    }

    /** 收音設定（C# 的 `audio_source`／`platform_effects`；手機包是 `Native.setMic` 一改就換；MR 包只從 `MicRecord.start` 帶進來）。 */
    static volatile String source = "voice_recognition";
    static volatile boolean effects = true;
    /** 插話（`kws_barge_in`）：只影響聾窗長度（{@link #deafWindowMs}）。 */
    static volatile boolean bargeIn = true;

    static int sourceId(String name) {
        Integer id = name == null ? null : SOURCES.get(name.trim());
        return id == null ? MediaRecorder.AudioSource.VOICE_RECOGNITION : id;
    }

    private final AudioRecord record;
    private final boolean wanted;
    private final List<AudioEffect> attached = new ArrayList<>();
    final boolean nsAvailable, agcAvailable, aecAvailable;
    boolean echoCancelled;

    MicFx(AudioRecord record, boolean wanted) {
        this.record = record;
        this.wanted = wanted;
        boolean ns = false, agc = false, aec = false;
        try {
            ns = NoiseSuppressor.isAvailable();
            agc = AutomaticGainControl.isAvailable();
            aec = AcousticEchoCanceler.isAvailable();
        } catch (Throwable ignored) {}
        nsAvailable = ns;
        agcAvailable = agc;
        aecAvailable = aec;
    }

    /** 裝置給了什麼（不管有沒有要）——診斷那一半。 */
    String describe() {
        return "NS=" + (nsAvailable ? "有" : "無") + " AGC=" + (agcAvailable ? "有" : "無")
            + " AEC=" + (aecAvailable ? "有" : "無");
    }

    /** 掛上有的、而且有要的；回真的掛上的名字（`MicEffects.attach()`）。 */
    List<String> attach() {
        List<String> names = new ArrayList<>();
        if (!wanted) return names;
        int session;
        try { session = record.getAudioSessionId(); } catch (Throwable t) { session = 0; }
        if (session == 0) return names;
        if (nsAvailable) {
            AudioEffect e = null;
            try { e = NoiseSuppressor.create(session); } catch (Throwable ignored) {}
            if (take(e)) names.add("NS");
        }
        if (agcAvailable) {
            AudioEffect e = null;
            try { e = AutomaticGainControl.create(session); } catch (Throwable ignored) {}
            if (take(e)) names.add("AGC");
        }
        if (aecAvailable) {
            AudioEffect e = null;
            try { e = AcousticEchoCanceler.create(session); } catch (Throwable ignored) {}
            if (take(e)) names.add("AEC");
        }
        echoCancelled = names.contains("AEC");
        return names;
    }

    private boolean take(AudioEffect e) {
        if (e == null) return false;
        try { e.setEnabled(true); } catch (Throwable ignored) {}
        attached.add(e);
        return true;
    }

    void release() {
        for (AudioEffect e : attached) { try { e.setEnabled(false); } catch (Throwable ignored) {} }
        for (AudioEffect e : attached) { try { e.release(); } catch (Throwable ignored) {} }
        attached.clear();
        echoCancelled = false;
    }

    static String joined(List<String> names) {
        if (names.isEmpty()) return "無";
        StringBuilder sb = new StringBuilder();
        for (String n : names) { if (sb.length() > 0) sb.append('+'); sb.append(n); }
        return sb.toString();
    }

    /**
     * 她最後一個音節之後麥克風還關多久（`ShellController.deafWindowMs`）：AEC 真的掛上 ⇒ 0；
     * 否則插話開著 350 ms、關著 900 ms（無回音消除時量過的安全值）。
     */
    static long deafWindowMs(boolean bargeIn, boolean echoCancelled) {
        if (echoCancelled) return 0L;
        return bargeIn ? 350L : 900L;
    }
}
