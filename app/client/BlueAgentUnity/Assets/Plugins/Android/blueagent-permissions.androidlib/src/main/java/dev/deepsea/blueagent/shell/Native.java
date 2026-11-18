package dev.deepsea.blueagent.shell;

import android.app.Activity;
import android.app.Application;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;

import org.json.JSONObject;

import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Unity 這一側叫得到的**唯一入口**（C# 包裝＝`Assets/Scripts/NativeShell.cs`）。
 *
 * 原 app 的這些能力都長在 Kotlin 外殼裡（`ShellController`／`MainActivity`）；Unity 版沒有那個外殼，
 * 所以把「要 Android API 的那幾件」原樣搬到這裡，其餘（紀錄、送話、狀態列）照舊在 C#：
 * <ul>
 *   <li>相册格子（{@link AlbumStore}）＋ 系統選圖（{@link PickerActivity}）＋ 原圖落地（{@link ImageStore}）；</li>
 *   <li>前景服務＋常駐通知（{@link ShellService}／{@link Notifications}）、リマインド通知；</li>
 *   <li>位置推送（{@link LocationPush}，`POST /v1/loc`）；</li>
 *   <li>前景收音（{@link MicRecord}：可選音源＋平台 NS／AGC／AEC，取代 Unity 的 `Microphone`）；</li>
 *   <li>熄屏時的耳朵（{@link BgEars}）：Unity 在 activity onStop 之後**整個主迴圈停住**
 *       （`Microphone`／`UnityWebRequest` 全停），前景服務只保得住行程、保不住收音 ⇒
 *       熄屏那段由這裡用 `AudioRecord` 接手上行。</li>
 * </ul>
 *
 * Java → C# 一律走**事件佇列**（{@link #poll}；C# 在 Update 裡拉）：不用 `AndroidJavaProxy`
 * 回呼——回呼落在 Java 執行緒，C# 那邊還得自己再排回主執行緒，而且 Unity 暫停時會堆在半路。
 * 佇列在 Unity 暫停時照樣收（熄屏期間耳朵收到的幀，亮屏後 C# 一次補進紀錄）。
 */
public final class Native {
    static final String TAG = "BlueAgent";

    private static final ConcurrentLinkedQueue<String> EVENTS = new ConcurrentLinkedQueue<>();
    static final ExecutorService WORKER = Executors.newSingleThreadExecutor();
    static final Handler MAIN = new Handler(Looper.getMainLooper());

    static Context app;
    static volatile String bridgeUrl = "";
    static volatile String bridgeToken = "";
    static volatile String deviceKey = "";

    private static boolean lifecycleHooked;
    // 哪幾個 activity 在 started（非空 ⇒ Unity 看得見）。用集合不用計數器（2026-10-18）：鎖屏底下啟動時 C# 叫
    // `init` 那一刻 activity 其實已經 onStop，計數器「假設 1」之後解鎖的 onStart 把它推到 2、熄屏只回到 1
    // ⇒ `BgEars.onBackground` 永遠不叫。集合裡同一個 activity 只算一次，重複的 onStart 不會把它算歪。
    private static final java.util.Set<Activity> started =
        java.util.Collections.newSetFromMap(new java.util.IdentityHashMap<>());
    private static LocationPush location;

    private Native() {}

    // ═════════════════════════════════════════════════════════════ 基本

    /** 開機叫一次（冪等）：記住 application context、掛 activity 生命週期（熄屏耳朵要知道 onStop）。 */
    public static void init(Activity activity) {
        if (activity == null) return;
        app = activity.getApplicationContext();
        if (lifecycleHooked) return;
        lifecycleHooked = true;
        started.add(activity);                // 叫這支的時候通常在前景；不是的話下一個 onStop／onStart 會校正
        activity.getApplication().registerActivityLifecycleCallbacks(new Application.ActivityLifecycleCallbacks() {
            @Override public void onActivityStarted(Activity a) {
                boolean was = !started.isEmpty();
                started.add(a);
                if (!was) BgEars.onForeground();
                if (!was) NoticeListen.onForeground();  // 回前景：通知交回 C# 那條推播
                SeeOverlay.appVisible(true);           // 「看截图」の球：自分のアプリでは出さない
            }
            @Override public void onActivityStopped(Activity a) {
                started.remove(a);
                if (started.isEmpty()) BgEars.onBackground();
                if (started.isEmpty()) NoticeListen.onBackground(a);   // 背景：私聊／帖子通知由這裡響
                if (started.isEmpty()) SeeOverlay.appVisible(false);
            }
            @Override public void onActivityCreated(Activity a, Bundle b) {}
            @Override public void onActivityResumed(Activity a) {}
            @Override public void onActivityPaused(Activity a) {}
            @Override public void onActivitySaveInstanceState(Activity a, Bundle b) {}
            @Override public void onActivityDestroyed(Activity a) {}
        });
        Log.i(TAG, "native：init（" + activity.getClass().getSimpleName() + "）");
    }

    /** 拉一筆事件（JSON 字串）；沒有＝null。 */
    public static String poll() {
        return EVENTS.poll();
    }

    static void emit(JSONObject event) {
        EVENTS.add(event.toString());
    }

    static JSONObject event(String type) {
        JSONObject o = new JSONObject();
        try { o.put("type", type); } catch (Exception ignored) {}
        return o;
    }

    /** 橋的位址／token／端末鍵（位置推送與熄屏耳朵直接打橋要用）。設定一改 C# 就再叫一次。 */
    /** 驗收包（C# `BA_SELFTEST`）開機時叫：熄屏耳朵才認 `bg_inject.wav`（外部評審 v2 A2）。 */
    public static void setSelfTest(boolean on) {
        BgEars.selfTest = on;
    }

    public static void configure(String url, String token, String device) {
        bridgeUrl = url == null ? "" : url;
        bridgeToken = token == null ? "" : token;
        deviceKey = device == null ? "" : device;
    }

    // ═════════════════════════════════════════════════════════════ 相册／傳圖

    /** 一頁相册（背景執行緒算縮圖）⇒ 事件 `albumPage`（`page` 欄＝{@link AlbumStore#page} 的 JSON）。 */
    public static void albumPage(final int offset, final int count) {
        final Context ctx = app;
        if (ctx == null) return;
        WORKER.execute(() -> {
            JSONObject e = event("albumPage");
            try {
                e.put("offset", offset);
                e.put("page", new JSONObject(AlbumStore.page(ctx, offset, count)));
            } catch (Exception ex) {
                Log.i(TAG, "相册：一頁失敗 " + ex.getMessage());
            }
            emit(e);
        });
    }

    /** 格子裡點的那一張 ⇒ 讀原圖落地（`sendAlbumImage` 的前半）⇒ 事件 `imageSaved`。 */
    public static void saveAlbumImage(final String id) {
        final Context ctx = app;
        if (ctx == null) return;
        WORKER.execute(() -> {
            long numeric;
            try { numeric = Long.parseLong(id); } catch (Exception ex) { emitSaved(ctx, null, "album", "st.image.badId"); return; }
            Uri uri = AlbumStore.uriOf(numeric);
            Log.i(TAG, "画像：相册 id=" + id + " を読む");
            String saved = ImageStore.saveUri(ctx, uri);
            if (saved == null) {
                // 有些 ROM 的縮圖路走不通，但原圖讀得到 ⇒ 自己解碼一次再存。
                android.graphics.Bitmap bmp = AlbumStore.decodeScaled(ctx, uri, 1600);
                if (bmp != null) saved = ImageStore.save(ctx, bmp);
            }
            emitSaved(ctx, saved, "album", "st.image.albumUnreadable");
        });
    }

    /** 「端末のアルバムを開く」⇒ 系統相册（{@link PickerActivity}）；回來的圖落地後 ⇒ 事件 `imageSaved`。 */
    public static void pickImage(Activity activity) {
        if (activity == null) return;
        BgEars.paused = true;                         // 選圖期間 activity 會 onStop：不是熄屏，別起耳朵（P1-3）
        activity.runOnUiThread(() -> {
            try {
                activity.startActivity(new Intent(activity, PickerActivity.class));
            } catch (Exception ex) {
                Log.i(TAG, "画像：相册を開けなかった " + ex.getMessage());
                BgEars.paused = false;
                emit(event("pickCancelled"));
            }
        });
    }

    /** {@link PickerActivity} 回來（在背景執行緒落地；做完叫 `done`）。 */
    static void onPicked(final Uri uri, final Runnable done) {
        final Context ctx = app;
        WORKER.execute(() -> {
            String id = ctx == null ? null : ImageStore.saveUri(ctx, uri);
            emitSaved(ctx, id, "pick", "st.image.unreadablePerm");
            MAIN.post(done);
        });
    }

    /** 「看截图」の切り抜きが落ちた（{@link SeeOverlay#handOff}）⇒ 傳圖路へ（caption 付き）。 */
    static void seeSaved(Context ctx, String id, String caption) {
        JSONObject e = event("imageSaved");
        try {
            e.put("source", "see");
            e.put("caption", caption == null ? "" : caption);
            if (id == null || ctx == null) {
                e.put("error", "st.shot.cropUnreadable");
            } else {
                e.put("id", id);
                e.put("path", ImageStore.file(ctx, id).getAbsolutePath());
                JSONObject media = ImageStore.mediaJson(ctx, id);
                if (media != null) e.put("media", media);
            }
        } catch (Exception ignored) {}
        emit(e);
    }

    /** 「看截图」のアクセシビリティが設定で有効になっているか。 */
    public static boolean seeEnabled() {
        return app != null && SeeAccessibilityService.isEnabled(app);
    }

    private static void emitSaved(Context ctx, String id, String source, String errorKey) {
        JSONObject e = event("imageSaved");
        try {
            e.put("source", source);
            if (id == null || ctx == null) {
                e.put("error", errorKey);
            } else {
                e.put("id", id);
                e.put("path", ImageStore.file(ctx, id).getAbsolutePath());
                JSONObject media = ImageStore.mediaJson(ctx, id);
                if (media != null) e.put("media", media);
            }
        } catch (Exception ignored) {}
        emit(e);
    }

    // ═════════════════════════════════════════════════════════════ 前景服務／通知

    /** `startShellService(note)`：前景服務起來（或換通知那一行字）。⚠️ 只能在前景叫（Android 14+）。 */
    public static void startShellService(Activity activity, String note) {
        if (activity == null) return;
        try {
            Intent intent = new Intent(activity, ShellService.class).putExtra(ShellService.EXTRA_NOTE, note);
            if (android.os.Build.VERSION.SDK_INT >= 26) activity.startForegroundService(intent);
            else activity.startService(intent);
        } catch (Exception ex) {
            Log.i(TAG, "前景服務：起不來 " + ex.getMessage());
        }
    }

    /**
     * 背景系統推送的設定（B.App.8）：App Server 位址、token、自己的帳號 id、C# 暫停那一刻的游標（event_id）。
     * C# 登入後與每次 `OnApplicationPause(true)` 叫；`on=false`（登出／關掉通知）⇒ 停。
     */
    public static void configureNotices(String url, String token, String account, String lastEventId,
                                        boolean on) {
        NoticeListen.configure(url, token, account, lastEventId, on);
    }

    // ═════════════════════════════════════════════════════════════ 登入 token（Keystore）

    /** 存登入 token 進 Keystore（{@link SecureToken}）；回 false＝這台機器用不了，C# 退回 PlayerPrefs。 */
    public static boolean secureTokenPut(String token) {
        return SecureToken.put(app, token);
    }

    public static String secureTokenGet() {
        return SecureToken.get(app);
    }

    public static boolean secureTokenClear() {
        return SecureToken.clear(app);
    }

    /** リマインドの一本（`Notification.notifyReminder`）。 */
    public static void notifyReminder(String title, String text) {
        Context ctx = app;
        if (ctx != null) Notifications.notifyReminder(ctx, title, text);
    }

    // ═════════════════════════════════════════════════════════════ 位置

    /** 位置推送開始（冪等；權限由 C# 先要好）。回 false＝沒權限／沒有 provider。 */
    public static boolean startLocation(final Activity activity) {
        if (activity == null) return false;
        final boolean[] ok = { false };
        Runnable run = () -> {
            if (location == null) location = new LocationPush(activity.getApplicationContext());
            ok[0] = location.start();
        };
        if (Looper.myLooper() == Looper.getMainLooper()) run.run();
        else {
            // LocationManager 要一顆 Looper；在 UI 執行緒上跑，等它回來（最多 2 秒）。
            final Object lock = new Object();
            final boolean[] done = { false };
            activity.runOnUiThread(() -> {
                try { run.run(); } finally { synchronized (lock) { done[0] = true; lock.notifyAll(); } }
            });
            synchronized (lock) {
                long until = System.currentTimeMillis() + 2000;
                while (!done[0] && System.currentTimeMillis() < until) {
                    try { lock.wait(200); } catch (InterruptedException ignored) { break; }
                }
            }
        }
        return ok[0];
    }

    // ═════════════════════════════════════════════════════════════ 熄屏耳朵

    /**
     * 收音開關與這一輪的會話參數（C# 的 `micOn` 一變、session／角色一換就叫）。
     * 只是「記住」：真的開 `AudioRecord` 要等 Unity 的 activity onStop（見 {@link BgEars}）。
     */
    public static void setEars(boolean on, String session, String soul, String mode, boolean tts) {
        BgEars.configure(on, session, soul, mode, tts);
    }

    /**
     * 收音設定（`audio_source`／`platform_effects`／`kws_barge_in`）——前景 {@link MicRecord} 與熄屏 {@link BgEars}
     * 同一組（原 app 只有一條收音路，設定只有一份）。
     */
    public static void setMic(String source, boolean effects, boolean bargeIn) {
        MicFx.source = MicFx.SOURCES.containsKey(source) ? source : "voice_recognition";
        MicFx.effects = effects;
        MicFx.bargeIn = bargeIn;
    }

    /**
     * 熄屏那段的喚醒網（{@link BgWake}）：與前景 `WakeNet` 同一組設定（`BridgeShell.SyncEars` 叫）。
     * `on`＝呼びかけ＋`kws_enabled`＋不是閘常開的模式；`usable`＝前景那顆網起來了、金標自檢沒失敗；
     * `character`＝前景真的在用的那顆模型（已套過「沒有就退回 aster」）。只是記住：下次熄屏才生效。
     */
    public static void setWake(boolean on, boolean usable, String modelsDir, String character,
                               float kwsThreshold, int consecutive, int decideEvery, int cooldownMs,
                               boolean gateOn, float speakerThreshold, String gateRule, int minEvalMs, int gateDelayMs,
                               String spkSherpa, float spkSherpaThreshold, String sherpaProvider, int sherpaThreads) {
        BgWake.Config c = new BgWake.Config();
        c.on = on;
        c.usable = usable;
        c.modelsDir = modelsDir == null ? "" : modelsDir;
        c.character = character == null ? "" : character;
        c.kwsThreshold = kwsThreshold;
        c.consecutive = consecutive;
        c.decideEvery = decideEvery;
        c.cooldownMs = cooldownMs;
        c.gateOn = gateOn;
        c.speakerThreshold = speakerThreshold;
        c.gateRule = gateRule == null || gateRule.isEmpty() ? BgWake.RULE_TAIL : gateRule;
        c.minEvalMs = minEvalMs;
        c.gateDelayMs = gateDelayMs;
        c.spkSherpa = spkSherpa == null ? "" : spkSherpa;
        c.spkSherpaThreshold = spkSherpaThreshold;
        c.sherpaProvider = sherpaProvider == null || sherpaProvider.isEmpty() ? "cpu" : sherpaProvider;
        c.sherpaThreads = Math.max(1, sherpaThreads);
        BgEars.wake = c;
    }

    /** 帶通（`voice_filter`／`voice_high_pass_hz`／`voice_low_pass_hz`）——熄屏那段照前景 `MicCapture` 濾。 */
    public static void setBand(boolean on, double highPassHz, double lowPassHz) {
        BgEars.bandOn = on;
        BgEars.bandHigh = highPassHz;
        BgEars.bandLow = lowPassHz;
    }

    /** 熄屏時她的音量（0..1；設定頁的音量滑桿）。 */
    public static void setVolume(float volume) {
        BgEars.volume = Math.max(0f, Math.min(1f, volume));
    }

    /** Unity が背景に回る：鳴っていた一句の殘り＋待ち行列（`paths` は改行區切り）を原生で鳴らし續ける。 */
    public static void handOverSpeech(String paths, int startMs) {
        java.util.List<java.io.File> files = new java.util.ArrayList<>();
        if (paths != null) for (String p : paths.split("\n")) if (!p.isEmpty()) files.add(new java.io.File(p));
        BgEars.handOver(files, startMs);
    }

    /** 背景中に屆いた回覆の一句（C# `BackgroundVoice`）：原生の播放隊列の**後ろ**に足す。 */
    public static void appendSpeech(String path) {
        if (path != null && !path.isEmpty()) BgEars.append(new java.io.File(path));
    }

    /** 前景に戻った：原生側で鳴り終わらなかった分 `{"pos":ms,"files":[...]}`（C# が續きを鳴らす）。 */
    public static String takeBackSpeech() {
        return BgEars.takeBack();
    }

    /** 熄屏那段的應答音（C# `SyncBgAck`）：`dir`＝當前角色那組匯出好的 wav；`on=false` ⇒ 不播。 */
    public static void setAck(boolean on, String dir) {
        BgEars.ackDir = on && dir != null ? dir : "";
    }
}
