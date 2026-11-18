package dev.deepsea.blueagent.shell;

import android.app.Activity;
import android.content.Intent;
import android.media.projection.MediaProjectionConfig;
import android.media.projection.MediaProjectionManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.util.Log;
import android.widget.Toast;

/**
 * 「看截图」路線 A の入口（透明・畫面なし；{@link SeeTile} から、または `am start`）。
 *
 * ① 「他のアプリの上に表示」がまだ ⇒ その設定畫面を開いて終わり（許可は本人が切り替える）；
 * ② 撮影中 ⇒ 止める（タイルの二度押し）；
 * ③ それ以外 ⇒ 系統の「画面の共有／録画」確認 ⇒ OK なら {@link SeeProjection} を起こす。
 * Android 14+ は「画面全体」を既定にする（`createConfigForDefaultDisplay`；単一アプリだと球の先の画面が撮れない）。
 */
public class SeeProjectionActivity extends Activity {
    private static final int REQ_CAPTURE = 4715;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        BgEars.paused = true;                       // これは熄屏ではない（PickerActivity と同じ）
        if (savedInstanceState != null) return;
        if (!Settings.canDrawOverlays(this)) {
            Log.i(Native.TAG, "看截图(A)：「他のアプリの上に表示」が未許可 ⇒ 設定を開く");
            Toast.makeText(this, "「他のアプリの上に表示」を許可してから、もう一度押してください", Toast.LENGTH_LONG).show();
            try {
                startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                    Uri.parse("package:" + getPackageName())).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            } catch (Exception e) {
                Log.i(Native.TAG, "看截图(A)：設定を開けなかった " + e.getMessage());
            }
            finish();
            return;
        }
        if (SeeProjection.running()) {
            SeeProjection.stop(this);
            finish();
            return;
        }
        MediaProjectionManager mpm = getSystemService(MediaProjectionManager.class);
        if (mpm == null) { finish(); return; }
        Intent ask = Build.VERSION.SDK_INT >= 34
            ? mpm.createScreenCaptureIntent(MediaProjectionConfig.createConfigForDefaultDisplay())
            : mpm.createScreenCaptureIntent();
        try {
            startActivityForResult(ask, REQ_CAPTURE);
        } catch (Exception e) {
            Log.i(Native.TAG, "看截图(A)：確認を出せなかった " + e.getMessage());
            finish();
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_CAPTURE) return;
        if (resultCode != RESULT_OK || data == null) {
            Log.i(Native.TAG, "看截图(A)：共有を斷られた");
            finish();
            return;
        }
        Intent start = new Intent(this, SeeProjection.class)
            .putExtra(SeeProjection.EXTRA_CODE, resultCode)
            .putExtra(SeeProjection.EXTRA_DATA, data);
        try {
            startForegroundService(start);
        } catch (Exception e) {
            Log.i(Native.TAG, "看截图(A)：サービスを起こせなかった " + e.getMessage());
        }
        finish();
    }

    @Override
    protected void onDestroy() {
        BgEars.paused = false;
        super.onDestroy();
    }
}
