package dev.deepsea.blueagent.shell;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.ext.SdkExtensions;
import android.provider.MediaStore;
import android.util.Log;

/**
 * 系統相册（原 app `MainActivity.pickImage`／`openGallery`／`onActivityResult`）。
 *
 * Unity 的 activity 收不到 `onActivityResult`（那是 Unity 自己的類），所以開一顆**透明、沒有畫面**的
 * activity 專門接結果。挑選器的順序（2026-10-29 owner：「圖片從相冊來，而不是文件來」）：
 * ① 系統的照片挑選器 `ACTION_PICK_IMAGES`（Android 13+ 內建；11／12 由系統更新補上）——一定是相冊；
 * ② `ACTION_PICK` 打 MediaStore——不少 ROM 會把它交給「文件」，所以只當退路；
 * ③ 文件選擇器（寧可多一步，也不要「按了沒反應」）。選到的圖**在這顆 activity 還活著的時候**落地
 * （URI 的臨時讀權綁在接收的 activity 上），做完才 finish。
 */
public class PickerActivity extends Activity {
    private static final int REQ_PICK_IMAGE = 4714;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        BgEars.paused = true;                            // 翻相册的整段不是熄屏（外部評審 P1-3）
        if (savedInstanceState != null) return;          // 被系統重建：上一次的請求還在路上
        Intent photos = new Intent(ACTION_PICK_IMAGES);
        photos.setType("image/*");
        Intent gallery = new Intent(Intent.ACTION_PICK, MediaStore.Images.Media.EXTERNAL_CONTENT_URI);
        Intent fallback = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        fallback.addCategory(Intent.CATEGORY_OPENABLE);
        fallback.setType("image/*");
        Intent target = photoPickerAvailable() && photos.resolveActivity(getPackageManager()) != null ? photos
                : gallery.resolveActivity(getPackageManager()) != null ? gallery : fallback;
        Log.i(Native.TAG, "画像：選択器 " + target.getAction());
        try {
            startActivityForResult(target, REQ_PICK_IMAGE);
        } catch (Exception e) {
            Log.i(Native.TAG, "画像：相册を開けなかった " + e.getMessage());
            Native.emit(Native.event("pickCancelled"));
            finish();
        }
    }

    /** `MediaStore.ACTION_PICK_IMAGES`（API 33 才有常數；寫字串，舊 compileSdk 也編得過）。 */
    private static final String ACTION_PICK_IMAGES = "android.provider.action.PICK_IMAGES";

    /** 系統照片挑選器在不在：13+ 內建；11／12 要系統模組更新到 R extension 2 以上。 */
    private static boolean photoPickerAvailable() {
        if (Build.VERSION.SDK_INT >= 33) return true;
        if (Build.VERSION.SDK_INT >= 30) {
            try {
                return SdkExtensions.getExtensionVersion(Build.VERSION_CODES.R) >= 2;
            } catch (Throwable ignored) {
                return false;
            }
        }
        return false;
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_PICK_IMAGE) return;
        Uri uri = data == null ? null : data.getData();
        if (resultCode != RESULT_OK || uri == null) {
            // 取消＝什麼都不做（框裡的字跟著這張圖走，已經在 C# 那側記著）。
            Native.emit(Native.event("pickCancelled"));
            finish();
            return;
        }
        Log.i(Native.TAG, "画像：選んだ " + uri);
        Native.onPicked(uri, this::finish);
    }

    @Override
    protected void onDestroy() {
        BgEars.paused = false;                           // 選完／取消／開不起來都會走到這裡
        super.onDestroy();
    }
}
