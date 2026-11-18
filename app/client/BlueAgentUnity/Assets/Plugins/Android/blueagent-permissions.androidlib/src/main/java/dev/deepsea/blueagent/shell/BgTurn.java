package dev.deepsea.blueagent.shell;

/**
 * 熄屏耳朵的「回合節奏」：共用層 `EarsStream`（Kotlin `EarsStream.kt` ／ C# `EarsStream.cs`）的 Java 移植。
 *
 * 規則**一字未動**（含刻意不直覺的那幾處：`close()` 不清 sentences、`arm()` 不清 preopen、送塊本身不續期
 * 會話窗）；拿掉的只有端點提示（`Level`／`Hint`）——前景那條也沒在送 `flush=1`。
 * 時間一律是參數（`now`，毫秒），不讀時鐘。呼叫端（{@link BgEars}）負責加鎖。
 */
final class BgTurn {
    static final int STREAM_BLOCK_MS = 500;
    static final long CONVERSATION_MS = 10000L;
    /** 預開閘沒有結論時手機自己的保險絲（橋側 `EARS_PREOPEN_MAX_MS`＝4 s，這裡寬一點）。 */
    static final long PREOPEN_MAX_MS = 6000L;
    static final long SPEECH_GRACE_MS = 3000L;

    boolean armed;
    int blocks;
    boolean inFlight;
    boolean delivered;
    int sentences;
    double lastScore = Double.NaN;
    long armedAt;
    long conversationUntil;
    long playbackUntil;
    boolean preopen;
    long preopenAt;
    int preopenBlocks;

    boolean canSend() {
        return armed && !inFlight;
    }

    void arm(long now) {
        armed = true;
        blocks = 0;
        inFlight = false;
        delivered = false;
        sentences = 0;
        lastScore = Double.NaN;
        armedAt = now;
        conversationUntil = now + CONVERSATION_MS;
        playbackUntil = 0L;
    }

    /** 本機喚醒網叫到名字（粗篩）：開一輪、開始送 `pre=1`，不出聲、不宣稱確認——等橋的 `gate`。 */
    void preopenEnter(long now) {
        arm(now);
        preopen = true;
        preopenAt = now;
        preopenBlocks = 0;
    }

    /** 橋開門。ack＝只有名字（窗從此刻起算）；false＝他接著還有話。 */
    void gateOpen(boolean ack, long now) {
        preopen = false;
        preopenAt = 0L;
        if (ack) conversationUntil = now + CONVERSATION_MS;
    }

    void gateReject() {
        close();
    }

    /** 橋從常駐訂閱說「這是他在說話」（`ears{kind:heard}`）：續期會話窗。刻意不動 inFlight。 */
    void peerHeard(long now) {
        if (!armed) return;
        conversationUntil = now + CONVERSATION_MS;
    }

    void conversationFromPlayback(long now, long audioMs) {
        long until = now + Math.max(audioMs, 0L) + CONVERSATION_MS;
        if (until > playbackUntil) playbackUntil = until;
    }

    boolean reopenAfterSpeech(long now) {
        if (playbackUntil == 0L && conversationUntil <= now) return false;
        long until = Math.max(now + CONVERSATION_MS, playbackUntil);
        arm(now);
        conversationUntil = until;
        playbackUntil = 0L;
        return true;
    }

    void holdOpen(long now, long capMs) {
        if (!armed) return;
        if (armedAt > 0L && now - armedAt > capMs) return;
        long until = now + CONVERSATION_MS;
        if (until > conversationUntil) conversationUntil = until;
    }

    boolean takeBlock() {
        if (!canSend()) return false;
        inFlight = true;
        blocks += 1;
        if (preopen) preopenBlocks += 1;
        return true;
    }

    boolean onReply(int sentenceCount, boolean deliveredNow, double score, long now, boolean hisHeard) {
        inFlight = false;
        if (sentenceCount > 0) sentences += sentenceCount;
        if (!Double.isNaN(score)) lastScore = score;
        if (deliveredNow) {
            delivered = true;
            long grace = now + SPEECH_GRACE_MS;
            if (grace > playbackUntil) playbackUntil = grace;
        } else if (hisHeard) {
            conversationUntil = now + CONVERSATION_MS;
        }
        return true;
    }

    boolean tick(long now) {
        if (!armed) return false;
        if (inFlight) return true;
        if (preopen) {
            if (now - preopenAt <= PREOPEN_MAX_MS) return true;
            close();
            return false;
        }
        if (delivered) {
            if (playbackUntil > 0L && now <= playbackUntil) return true;
            if (!reopenAfterSpeech(now)) { close(); return false; }
        }
        if (conversationUntil > 0L && now > conversationUntil) {
            close();
            return false;
        }
        return true;
    }

    boolean onStreamFailed() {
        inFlight = false;
        close();
        return false;
    }

    void close() {
        armed = false;
        inFlight = false;
        delivered = false;
        preopen = false;
        preopenAt = 0L;
        blocks = 0;
        armedAt = 0L;
        conversationUntil = 0L;
        playbackUntil = 0L;
    }
}
