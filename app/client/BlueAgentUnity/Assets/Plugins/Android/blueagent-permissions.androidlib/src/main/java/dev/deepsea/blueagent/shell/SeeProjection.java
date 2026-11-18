package dev.deepsea.blueagent.shell;

import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.graphics.Bitmap;
import android.graphics.PixelFormat;
import android.graphics.Rect;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.Looper;
import android.util.Log;
import android.view.WindowManager;

import java.nio.ByteBuffer;

/**
 * 「看截图」**路線 A**（2026-10-19 原型）：無障礙を使わない目。
 *
 * 球＝「他のアプリの上に表示」（`SYSTEM_ALERT_WINDOW`、`TYPE_APPLICATION_OVERLAY`）；
 * 撮る＝`MediaProjection`。同意は**セッションに一回**（{@link SeeProjectionActivity} が系統の確認を出す）：
 * 同意の token は一回しか使えない（Android 14+）ので、VirtualDisplay＋ImageReader を**開きっぱなし**にして、
 * 球を押したらいちばん新しいフレームを一枚取る。止める＝通知の「やめる」／タイル／系統の「共有を停止」。
 *
 * 代償（無障礙版と比べて）：撮影中はステータスバーに「画面共有中」の印が出続ける；Android 15+ は
 * 共有中にワンタイムコード等の通知を隠す；回転には追従しない（原型：開始時の向きの大きさで撮る）。
 * 球・圈選・送る路は {@link SeeOverlay}（無障礙版と同じもの）。
 */
public final class SeeProjection extends Service {
    static final String EXTRA_CODE = "code";
    static final String EXTRA_DATA = "data";
    static final String ACTION_STOP = "dev.deepsea.blueagent.see.STOP";

    private static volatile SeeProjection live;

    static boolean running() { return live != null; }

    private final Handler main = new Handler(Looper.getMainLooper());
    private MediaProjection projection;
    private VirtualDisplay display;
    private ImageReader reader;
    private HandlerThread thread;
    private Handler frames;
    private Image latest;                 // いちばん新しいフレーム（frames スレッドだけが觸る）
    private int width, height;

    @Override public IBinder onBind(Intent intent) { return null; }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null || ACTION_STOP.equals(intent.getAction())) {
            Log.i(Native.TAG, "看截图(A)：やめる");
            stopSelf();
            return START_NOT_STICKY;
        }
        if (projection != null) return START_NOT_STICKY;      // もう撮影中
        Notifications.center(this);
        Intent stop = new Intent(this, SeeProjection.class).setAction(ACTION_STOP);
        PendingIntent stopPi = PendingIntent.getService(this, 1, stop, PendingIntent.FLAG_IMMUTABLE);
        try {
            // ⚠️ 順番：mediaProjection 型の前景服務を**先に**立ててから getMediaProjection（Android 14+）。
            if (Build.VERSION.SDK_INT >= 29) {
                startForeground(Notifications.SEE_ID, Notifications.seeSession(this, stopPi),
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION);
            } else {
                startForeground(Notifications.SEE_ID, Notifications.seeSession(this, stopPi));
            }
            int code = intent.getIntExtra(EXTRA_CODE, 0);
            Intent data = Build.VERSION.SDK_INT >= 33
                ? intent.getParcelableExtra(EXTRA_DATA, Intent.class)
                : legacyData(intent);
            MediaProjectionManager mpm = getSystemService(MediaProjectionManager.class);
            projection = mpm == null || data == null ? null : mpm.getMediaProjection(code, data);
            if (projection == null) throw new IllegalStateException("projection が取れない");
            // Android 14+：createVirtualDisplay の前に callback を登録しないと例外。
            projection.registerCallback(new MediaProjection.Callback() {
                @Override public void onStop() {
                    Log.i(Native.TAG, "看截图(A)：系統側で共有が止まった");
                    main.post(SeeProjection.this::stopSelf);
                }
            }, main);
            openDisplay();
        } catch (Exception e) {
            Log.i(Native.TAG, "看截图(A)：開始できなかった " + e.getMessage());
            stopSelf();
            return START_NOT_STICKY;
        }
        live = this;
        SeeOverlay.attach(this, WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY, this::shot);
        Log.i(Native.TAG, "看截图(A)：撮影セッション開始 " + width + "x" + height + "（球を出す）");
        return START_NOT_STICKY;
    }

    @SuppressWarnings("deprecation")
    private static Intent legacyData(Intent intent) { return intent.getParcelableExtra(EXTRA_DATA); }

    private void openDisplay() {
        WindowManager wm = getSystemService(WindowManager.class);
        Rect bounds = wm.getMaximumWindowMetrics().getBounds();
        width = bounds.width();
        height = bounds.height();
        int dpi = getResources().getDisplayMetrics().densityDpi;
        thread = new HandlerThread("ba-see-frames");
        thread.start();
        frames = new Handler(thread.getLooper());
        // maxImages 3：`latest` で一枚握っていても acquireLatestImage が回る。
        reader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 3);
        reader.setOnImageAvailableListener(r -> {
            Image next = null;
            try { next = r.acquireLatestImage(); } catch (Exception ignored) {}
            if (next == null) return;
            if (latest != null) latest.close();
            latest = next;
        }, frames);
        display = projection.createVirtualDisplay("blueagent-see", width, height, dpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, reader.getSurface(), null, frames);
    }

    /** 球が隠れた後（{@link SeeOverlay} が 140 ms 待つ）のいちばん新しいフレーム → Bitmap。 */
    private void shot(SeeAccessibilityService.ShotCallback cb) {
        Handler h = frames;
        if (h == null) { cb.done(null); return; }
        h.post(() -> {
            Image img = latest;
            if (img == null) { cb.done(null); return; }
            try {
                Image.Plane plane = img.getPlanes()[0];
                ByteBuffer buf = plane.getBuffer();
                int pixelStride = plane.getPixelStride();
                int rowStride = plane.getRowStride();
                int padded = rowStride / pixelStride;
                Bitmap wide = Bitmap.createBitmap(padded, height, Bitmap.Config.ARGB_8888);
                buf.rewind();
                wide.copyPixelsFromBuffer(buf);
                Bitmap bmp = padded == width ? wide : Bitmap.createBitmap(wide, 0, 0, width, height);
                cb.done(bmp);
            } catch (Exception e) {
                Log.i(Native.TAG, "看截图(A)：フレームを讀めなかった " + e.getMessage());
                cb.done(null);
            }
        });
    }

    @Override
    public void onDestroy() {
        live = null;
        SeeOverlay.detach(this);
        if (display != null) display.release();
        display = null;
        if (projection != null) { try { projection.stop(); } catch (Exception ignored) {} }
        projection = null;
        Handler h = frames;
        if (h != null) h.post(() -> {
            if (latest != null) latest.close();
            latest = null;
            if (reader != null) reader.close();
            reader = null;
            thread.quitSafely();
        });
        frames = null;
        super.onDestroy();
    }

    static void stop(Context ctx) {
        ctx.startService(new Intent(ctx, SeeProjection.class).setAction(ACTION_STOP));
    }
}
