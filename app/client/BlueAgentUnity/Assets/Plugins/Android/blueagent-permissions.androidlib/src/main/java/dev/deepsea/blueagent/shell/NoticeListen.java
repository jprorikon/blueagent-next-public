package dev.deepsea.blueagent.shell;

import android.content.Context;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * **App 在背景時的系統推送**（owner 2026-11-16 定 B.App.8：App 內提醒條＋系統推送，先只做 Android）。
 *
 * 為什麼在 Java：Unity 一 onStop 主迴圈就停，C# 那條 `/v1/events` 常駐推播跟著停 ⇒ 她私聊你、
 * 別人回你的帖，手機都不會響。這裡在**最後一個 activity onStop** 時接手，用同一條 SSE
 * （`Accept: text/event-stream`、`Last-Event-ID`＝C# 暫停那一刻的游標）只聽兩種事件：
 * <ul>
 *   <li>`notice.created`：別人回了／讚了你的帖（App Server 只給帖主，B.App.8）；</li>
 *   <li>`chat.message.created`：一對一私聊、作者不是你自己。群聊不響（她們三人自己會聊很多）。</li>
 *   <li>`call.incoming`／`call.updated`：她打給你（owner 2026-11-17）。響鈴中彈一則「來電」通知（30 秒後自動消失），
 *       接了／拒了／她掛了／逾時 ⇒ 收掉那一則。點開 App 時若還在響，C# 會顯示全屏來電畫面
 *       （`NextIncomingCall.Check`）。</li>
 * </ul>
 * 回到前景（任一 activity onStart）就停；游標**不回寫**給 C#——C# 從自己的游標重放、照常畫未讀，
 * 這裡只負責「響」。不走 FCM：沒有 Google 服務依賴，代價是行程被系統收掉之後就不響
 * （常駐前景服務 {@link ShellService} 開著、或設成主屏 app 時行程留得住）。
 */
final class NoticeListen {
    private static final String TAG = Native.TAG;
    /** 伺服器 15 秒一個 keepalive；45 秒沒動靜＝線死了，重連。 */
    private static final int READ_TIMEOUT_MS = 45000;
    private static final long MAX_BACKOFF_MS = 60000L;

    private static volatile String base = "";
    private static volatile String token = "";
    private static volatile String me = "";
    private static volatile String cursor = "";
    private static volatile boolean enabled;
    private static Thread thread;
    private static volatile boolean running;
    private static volatile HttpURLConnection current;

    private NoticeListen() {}

    /** C# 登入後與每次暫停前叫：位址、token、自己的帳號 id、暫停那一刻的事件游標（event_id）。 */
    static void configure(String url, String bearer, String account, String lastEventId, boolean on) {
        base = url == null ? "" : url.trim();
        while (base.endsWith("/")) base = base.substring(0, base.length() - 1);
        token = bearer == null ? "" : bearer;
        me = account == null ? "" : account;
        cursor = lastEventId == null ? "" : lastEventId;
        enabled = on && !base.isEmpty() && !token.isEmpty();
        if (!enabled) onForeground();
    }

    /** 最後一個 activity onStop。 */
    static synchronized void onBackground(final Context context) {
        // 沒有游標就不接：從頭讀會把整份事件日誌當新通知全響一遍
        if (!enabled || running || context == null || cursor.isEmpty()) return;
        running = true;
        final Context app = context.getApplicationContext();
        thread = new Thread(() -> loop(app), "ba-notice-listen");
        thread.setDaemon(true);
        thread.start();
        Log.i(TAG, "notice：背景接手（after=" + cursor + "）");
    }

    /** 任一 activity onStart，或關掉通知。 */
    static synchronized void onForeground() {
        if (!running) return;
        running = false;
        HttpURLConnection c = current;
        if (c != null) {
            try { c.disconnect(); } catch (Exception ignored) {}
        }
        if (thread != null) thread.interrupt();
        thread = null;
        Log.i(TAG, "notice：回前景，交回 C#");
    }

    private static void loop(Context app) {
        long backoff = 2000L;
        while (running) {
            int code = 0;
            try {
                code = listen(app);
                backoff = 2000L;                       // 連得上就從頭算
            } catch (Exception e) {
                if (!running) break;
                Log.i(TAG, "notice：斷線 " + e.getClass().getSimpleName());
            }
            if (code == 401 || code == 403) {          // token 失效：等 C# 重新登入再設定
                enabled = false;
                running = false;
                break;
            }
            if (code == 409) {                         // 游標過期：不從頭重放（會全響），這段背景就不響了
                running = false;
                break;
            }
            try {
                Thread.sleep(backoff);
            } catch (InterruptedException e) {
                break;
            }
            backoff = Math.min(MAX_BACKOFF_MS, backoff * 2);
        }
    }

    /** 開一條 SSE，讀到斷為止；回 HTTP code（非 200 時）。 */
    private static int listen(Context app) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(base + "/v1/events").openConnection();
        current = c;
        try {
            c.setConnectTimeout(8000);
            c.setReadTimeout(READ_TIMEOUT_MS);
            c.setRequestProperty("Authorization", "Bearer " + token);
            c.setRequestProperty("Accept", "text/event-stream");
            c.setRequestProperty("X-Source", "android-notice");
            if (!cursor.isEmpty()) c.setRequestProperty("Last-Event-ID", cursor);
            int code = c.getResponseCode();
            if (code != 200) return code;
            try (BufferedReader in = new BufferedReader(
                    new InputStreamReader(c.getInputStream(), StandardCharsets.UTF_8))) {
                String id = "";
                String type = "";
                StringBuilder data = new StringBuilder();
                String line;
                while (running && (line = in.readLine()) != null) {
                    if (line.isEmpty()) {              // 一幀結束
                        if (data.length() > 0) dispatch(app, id, type, data.toString());
                        id = "";
                        type = "";
                        data.setLength(0);
                    } else if (line.startsWith("id:")) {
                        id = line.substring(3).trim();
                    } else if (line.startsWith("event:")) {
                        type = line.substring(6).trim();
                    } else if (line.startsWith("data:")) {
                        if (data.length() > 0) data.append('\n');
                        data.append(line.substring(5).trim());
                    }
                }
            }
            return 200;
        } finally {
            current = null;
            c.disconnect();
        }
    }

    private static void dispatch(Context app, String id, String type, String data) {
        if (!id.isEmpty()) cursor = id;                // 重連從這裡接（只在這條線裡用）
        String[] note;
        String[] call;
        try {
            JSONObject event = new JSONObject(data);
            call = callFor(type, event);
            note = call == null ? noteFor(type, event, me) : null;
        } catch (Exception e) {
            return;                                    // 壞包：跳過，不卡住整條線
        }
        if (call != null) {
            if ("ring".equals(call[0])) {
                long timeout = 30000L;
                try { timeout = Math.max(5000L, (long) (Double.parseDouble(call[4]) * 1000)); }
                catch (Exception ignored) {}
                Notifications.notifyCall(app, call[1], call[2], call[3], timeout);
            } else {
                Notifications.cancelCall(app, call[1]);
            }
            return;
        }
        if (note != null) Notifications.notifyMessage(app, id.isEmpty() ? data : id, note[0], note[1]);
    }

    /**
     * 她打來的電話：回 {"ring"|"clear", call_id, 標題, 本文, 響幾秒}；不是來電事件回 null。純函式（好測）。
     */
    static String[] callFor(String type, JSONObject event) {
        if (!"call.incoming".equals(type) && !"call.updated".equals(type)) return null;
        JSONObject payload = event.optJSONObject("payload");
        if (payload == null) return null;
        String callId = payload.optString("call_id", "");
        if (callId.isEmpty()) return null;
        String who = payload.optString("display", payload.optString("entity", ""));
        boolean ringing = "call.incoming".equals(type) && "ringing".equals(payload.optString("state"));
        return new String[] { ringing ? "ring" : "clear", callId, who, "着信中…",
                              String.valueOf(payload.optDouble("ring_s", 30.0)) };
    }

    /** 這一幀要不要響、響什麼：回 {標題, 本文}；不響回 null。純函式（好測）。 */
    static String[] noteFor(String type, JSONObject event, String self) {
        JSONObject payload = event.optJSONObject("payload");
        if (payload == null) return null;
        if ("notice.created".equals(type)) {
            String who = payload.optString("actor_display", payload.optString("actor_id", ""));
            if ("reply".equals(payload.optString("kind"))) {
                return new String[] { who, "返信：" + payload.optString("preview", "") };
            }
            if ("like".equals(payload.optString("kind"))) {
                return new String[] { who, "いいね：" + payload.optString("post_preview", "") };
            }
            return null;
        }
        if ("chat.message.created".equals(type)) {
            if (!"direct".equals(payload.optString("conversation_kind"))) return null;
            JSONObject message = payload.optJSONObject("message");
            if (message == null || message.optBoolean("deleted")) return null;
            JSONObject author = message.optJSONObject("author");
            String authorId = author == null ? "" : author.optString("id", "");
            if (authorId.isEmpty() || authorId.equals(self)) return null;
            String who = author.optString("display", authorId);
            return new String[] { who, preview(message.optJSONArray("parts")) };
        }
        return null;
    }

    private static String preview(JSONArray parts) {
        if (parts == null) return "";
        StringBuilder out = new StringBuilder();
        for (int i = 0; i < parts.length(); i++) {
            JSONObject part = parts.optJSONObject(i);
            if (part == null) continue;
            String kind = part.optString("kind");
            String text = "text".equals(kind) ? part.optString("text", "")
                : "photo".equals(kind) ? "［写真］"
                : "sticker".equals(kind) ? "［スタンプ］"
                : "call".equals(kind) ? "不在着信" : "";
            if (text.isEmpty()) continue;
            if (out.length() > 0) out.append(' ');
            out.append(text);
        }
        String all = out.toString();
        return all.length() > 120 ? all.substring(0, 120) + "…" : all;
    }
}
