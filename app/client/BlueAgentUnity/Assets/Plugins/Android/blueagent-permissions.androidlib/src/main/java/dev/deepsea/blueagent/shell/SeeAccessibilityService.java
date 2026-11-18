package dev.deepsea.blueagent.shell;

import android.accessibilityservice.AccessibilityService;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.os.Build;
import android.provider.Settings;
import android.util.Log;
import android.view.Display;
import android.view.accessibility.AccessibilityEvent;

/**
 * 「看截图」の**目**（原 app `SeeAccessibilityService.kt` の逐行移植）：スマホの画面を撮るためだけの
 * アクセシビリティサービス。`AccessibilityService.takeScreenshot()`（API 30+）は設定で一度有効にすれば
 * 以後は無確認で撮れる（`MediaProjection` は撮るたびに確認ダイアログ）。
 *
 * イベントは「いま前景は誰か」だけ見る（球の出し分け）；画面の中身は読まない
 * （`canRetrieveWindowContent=false`）。有効になると {@link SeeOverlay} が浮き球を出す。
 */
public final class SeeAccessibilityService extends AccessibilityService {

    private static volatile SeeAccessibilityService live;

    static SeeAccessibilityService instance() { return live; }

    /** 設定で有効になっているか（自分のプロセスが bind 済みかとは別）。 */
    public static boolean isEnabled(Context ctx) {
        String mine = ctx.getPackageName() + "/" + SeeAccessibilityService.class.getName();
        int on = 0;
        try { on = Settings.Secure.getInt(ctx.getContentResolver(), Settings.Secure.ACCESSIBILITY_ENABLED); }
        catch (Exception ignored) {}
        if (on != 1) return false;
        String list = null;
        try { list = Settings.Secure.getString(ctx.getContentResolver(), Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES); }
        catch (Exception ignored) {}
        if (list == null) return false;
        for (String item : list.split(":")) if (item.equalsIgnoreCase(mine)) return true;
        return false;
    }

    @Override protected void onServiceConnected() {
        super.onServiceConnected();
        live = this;
        Log.i(Native.TAG, "看截图：アクセシビリティ有効（浮き球を出す）");
        SeeOverlay.attach(this);
    }

    @Override public boolean onUnbind(Intent intent) {
        Log.i(Native.TAG, "看截图：サービスが unbind された（球をしまう）");
        live = null;
        SeeOverlay.detach(this);
        return super.onUnbind(intent);
    }

    @Override public void onDestroy() {
        live = null;
        SeeOverlay.detach(this);
        super.onDestroy();
    }

    /** 「現在前台是誰」だけ（我們自己的 app 裡不要有球）。事件の中身は見ない。 */
    @Override public void onAccessibilityEvent(AccessibilityEvent event) {
        CharSequence pkg = event == null ? null : event.getPackageName();
        SeeOverlay.onForegroundPackage(pkg == null ? "" : pkg.toString());
    }

    @Override public void onInterrupt() {}

    interface ShotCallback { void done(Bitmap bmp); }

    /** いまの画面を一枚。ハードウェアバッファは必ず閉じる。失敗（FLAG_SECURE 等）は null。 */
    void shot(final ShotCallback cb) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            Log.i(Native.TAG, "看截图：API " + Build.VERSION.SDK_INT + " では takeScreenshot が無い");
            cb.done(null);
            return;
        }
        try {
            takeScreenshot(Display.DEFAULT_DISPLAY, getMainExecutor(), new TakeScreenshotCallback() {
                @Override public void onSuccess(ScreenshotResult result) {
                    Bitmap soft = null;
                    try {
                        Bitmap hw = Bitmap.wrapHardwareBuffer(result.getHardwareBuffer(), result.getColorSpace());
                        if (hw != null) soft = hw.copy(Bitmap.Config.ARGB_8888, false);
                    } catch (Exception exc) {
                        Log.i(Native.TAG, "看截图：Bitmap 化に失敗 " + exc.getMessage());
                    } finally {
                        try { result.getHardwareBuffer().close(); } catch (Exception ignored) {}
                    }
                    cb.done(soft);
                }

                @Override public void onFailure(int errorCode) {
                    Log.i(Native.TAG, "看截图：takeScreenshot 失敗 code=" + errorCode);
                    cb.done(null);
                }
            });
        } catch (Exception exc) {
            Log.i(Native.TAG, "看截图：takeScreenshot 例外 " + exc.getMessage());
            cb.done(null);
        }
    }
}
