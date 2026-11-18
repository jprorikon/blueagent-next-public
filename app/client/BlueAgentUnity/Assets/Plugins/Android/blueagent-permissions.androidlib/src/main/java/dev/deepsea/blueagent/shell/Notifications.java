package dev.deepsea.blueagent.shell;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

import dev.deepsea.blueagent.permissions.R;

/**
 * 外殼的通知（原 app `Notification.kt`）。兩條**不同的頻道**：
 * ①收音中（前景服務那一行；IMPORTANCE_LOW、ongoing、消不掉）
 * ②リマインド（IMPORTANCE_HIGH、有聲／震動、按了就消失）——混在同一條頻道，必有一邊的性質被犧牲。
 */
final class Notifications {
    static final int ID = 1001;
    private static final String CHANNEL = "blueagent-shell";
    private static final String REMIND_CHANNEL = "blueagent-remind";
    private static final int REMIND_ID = 1002;

    private Notifications() {}

    /** 點通知 ⇒ 回到 app（Unity 的入口 activity；不寫死類名，取套件的啟動 intent）。 */
    private static PendingIntent open(Context context) {
        Intent launch = context.getPackageManager().getLaunchIntentForPackage(context.getPackageName());
        if (launch == null) launch = new Intent();
        launch.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED);
        return PendingIntent.getActivity(context, 0, launch,
            PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    @SuppressWarnings("deprecation")
    private static android.app.Notification.Builder builder(Context context, String channel) {
        return Build.VERSION.SDK_INT >= 26
            ? new android.app.Notification.Builder(context, channel)
            : new android.app.Notification.Builder(context);
    }

    static android.app.Notification build(Context context, String title, String text) {
        return builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_blueagent)
            .setContentTitle(title)
            .setContentText(text)
            .setOngoing(true)
            .setContentIntent(open(context))
            .build();
    }

    /** 「看截图」（路線 A）の撮影セッション中の一行：MediaProjection 型の前景服務に要る＋「やめる」で止める。 */
    static final int SEE_ID = 1003;

    static android.app.Notification seeSession(Context context, PendingIntent stop) {
        return builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_blueagent)
            .setContentTitle("BlueAgent")
            .setContentText("看截图：浮き球で今の画面を見せられます")
            .setOngoing(true)
            .setContentIntent(open(context))
            .addAction(new android.app.Notification.Action.Builder(null, "やめる", stop).build())
            .build();
    }

    static void center(Context context) {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        if (manager == null) return;
        NotificationChannel channel = new NotificationChannel(CHANNEL,
            context.getString(R.string.notif_channel_name), NotificationManager.IMPORTANCE_LOW);
        channel.setDescription(context.getString(R.string.notif_channel_desc));
        channel.setShowBadge(false);
        manager.createNotificationChannel(channel);
    }

    private static void remindChannel(Context context) {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        if (manager == null || manager.getNotificationChannel(REMIND_CHANNEL) != null) return;
        NotificationChannel channel = new NotificationChannel(REMIND_CHANNEL,
            context.getString(R.string.notif_remind_channel_name), NotificationManager.IMPORTANCE_HIGH);
        channel.setDescription(context.getString(R.string.notif_remind_channel_desc));
        channel.setShowBadge(true);
        manager.createNotificationChannel(channel);
    }

    /**
     * 背景時の一本（B.App.8：私聊／自分の投稿への返信・いいね）。リマインドと同じ頻道（音・震動あり）だが、
     * **一件ごとに別 id**（`key`＝event_id のハッシュ）——続けて来ても前のを上書きしない。
     */
    static void notifyMessage(Context context, String key, String title, String text) {
        remindChannel(context);
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        if (manager == null) return;
        android.app.Notification note = builder(context, REMIND_CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_blueagent)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(new android.app.Notification.BigTextStyle().bigText(text))
            .setAutoCancel(true)
            .setContentIntent(open(context))
            .build();
        int id = 2000 + ((key == null ? 0 : key.hashCode()) & 0x3fffffff) % 100000;
        try {
            manager.notify(id, note);              // 許可が無い端末では靜かに何もしない
        } catch (Exception ignored) {}
    }

    /**
     * 彼女からの着信（owner 2026-11-17）：call_id ごとに一本、`timeoutMs` で自動的に消える（App Server が 30 秒で打ち切る）。
     * 押すと App が開き、まだ鳴っていれば C# が全画面の着信画面を出す。
     */
    static void notifyCall(Context context, String callId, String title, String text, long timeoutMs) {
        remindChannel(context);
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        if (manager == null) return;
        android.app.Notification.Builder builder = builder(context, REMIND_CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_blueagent)
            .setContentTitle(title)
            .setContentText(text)
            .setCategory(android.app.Notification.CATEGORY_CALL)
            .setAutoCancel(true)
            .setContentIntent(open(context));
        if (Build.VERSION.SDK_INT >= 26) builder.setTimeoutAfter(timeoutMs);
        try {
            manager.notify(callNoteId(callId), builder.build());   // 許可が無い端末では靜かに何もしない
        } catch (Exception ignored) {}
    }

    /** 着信が終わった（出た／拒否／彼女が切った／時間切れ）：その一本を消す。 */
    static void cancelCall(Context context, String callId) {
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        if (manager == null) return;
        try {
            manager.cancel(callNoteId(callId));
        } catch (Exception ignored) {}
    }

    private static int callNoteId(String callId) {
        return 200000 + ((callId == null ? 0 : callId.hashCode()) & 0x3fffffff) % 100000;   // 私聊（2000〜）と重ならない
    }

    /** リマインドの一本（`title`＝彼女の表示名、`text`＝彼女が言う一句）。 */
    static void notifyReminder(Context context, String title, String text) {
        remindChannel(context);
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        if (manager == null) return;
        android.app.Notification note = builder(context, REMIND_CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_blueagent)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(new android.app.Notification.BigTextStyle().bigText(text))
            .setAutoCancel(true)
            .setContentIntent(open(context))
            .build();
        try {
            manager.notify(REMIND_ID, note);      // 許可が無い端末では靜かに何もしない
        } catch (Exception ignored) {}
    }
}
