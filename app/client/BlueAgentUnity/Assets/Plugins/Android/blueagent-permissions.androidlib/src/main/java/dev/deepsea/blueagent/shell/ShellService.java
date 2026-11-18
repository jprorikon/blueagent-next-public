package dev.deepsea.blueagent.shell;

import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

/**
 * 前景服務（原 app `ShellService.kt`）：讓熄屏之後行程與收音還活著，通知那一行＝他手上的把手。
 *
 * Android 14+：`microphone` 型的前景服務**只能從前景啟動**（開機廣播也不行），而且要已經有
 * RECORD_AUDIO ⇒ 只由 C#（Unity activity 在前景時）叫 {@link Native#startShellService}。
 * 起不來（沒有麥克風權限、系統不准）就安靜地收掉——不影響任何既有功能。
 *
 * ⚠️ 只保得住行程：Unity 熄屏時主迴圈照樣停 ⇒ 熄屏那一段的收音是 {@link BgEars} 在做。
 */
public class ShellService extends Service {
    public static final String EXTRA_NOTE = "note";

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Notifications.center(this);
        String note = intent == null ? null : intent.getStringExtra(EXTRA_NOTE);
        if (note == null || note.isEmpty()) note = "稼働中";
        android.app.Notification n = Notifications.build(this, "BlueAgent", note);
        try {
            if (Build.VERSION.SDK_INT >= 29) {
                startForeground(Notifications.ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE);
            } else {
                startForeground(Notifications.ID, n);
            }
            Log.i(Native.TAG, "前景服務：" + note);
        } catch (Exception e) {
            Log.i(Native.TAG, "前景服務：startForeground 失敗 " + e.getMessage());
            stopSelf();
            return START_NOT_STICKY;
        }
        // ⚠️ 不用 START_STICKY（外部評審 P2-1）：麥克風型前景服務在 Android 14+ 只能從前景起；行程被殺後系統
        //    自動重啟它 ⇒ `startForeground(TYPE_MICROPHONE)` 必拋 ⇒ 白跑一輪。app 下次到前景時自己會再起。
        return START_NOT_STICKY;
    }
}
