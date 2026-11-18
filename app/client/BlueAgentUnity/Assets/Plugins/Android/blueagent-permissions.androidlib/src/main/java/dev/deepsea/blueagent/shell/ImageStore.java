package dev.deepsea.blueagent.shell;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.Uri;
import android.util.Base64;
import android.util.Log;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.util.Arrays;
import java.util.HashMap;

/**
 * 我這邊送出去的圖——原 app `ImageStore.kt` 的逐條移植。
 *
 * ①**原圖落地**（長邊 ≤ 1600 的 JPEG，`files/imgs/<id>.jpg`；C# 讀它轉 base64 帶進 `/chat` 的 `image`）
 * ②**縮圖 data URL**（長邊 ≤ 240，掛在紀錄那一列的 `media`）。落地目錄只留最近 40 張。
 */
final class ImageStore {
    private static final String TAG = Native.TAG;
    private static final int MAX_EDGE = 1600;
    private static final int THUMB_EDGE = 240;
    private static final int Q = 88;
    private static final int KEEP = 40;

    private static final HashMap<String, String> THUMBS = new HashMap<>();

    private ImageStore() {}

    private static File dir(Context ctx) {
        File d = new File(ctx.getFilesDir(), "imgs");
        d.mkdirs();
        return d;
    }

    static File file(Context ctx, String id) {
        return new File(dir(ctx), id + ".jpg");
    }

    private static Bitmap scale(Bitmap bmp, int maxEdge) {
        int longEdge = Math.max(bmp.getWidth(), bmp.getHeight());
        if (longEdge <= maxEdge) return bmp;
        float k = maxEdge / (float) longEdge;
        return Bitmap.createScaledBitmap(bmp, Math.max(1, (int) (bmp.getWidth() * k)),
            Math.max(1, (int) (bmp.getHeight() * k)), true);
    }

    private static byte[] jpeg(Bitmap bmp, int q) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        bmp.compress(Bitmap.CompressFormat.JPEG, q, out);
        return out.toByteArray();
    }

    private static void prune(Context ctx) {
        File[] files = dir(ctx).listFiles();
        if (files == null || files.length <= KEEP) return;
        Arrays.sort(files, (a, b) -> Long.compare(a.lastModified(), b.lastModified()));
        for (int i = 0; i < files.length - KEEP; i++) {
            String name = files[i].getName();
            THUMBS.remove(name.endsWith(".jpg") ? name.substring(0, name.length() - 4) : name);
            files[i].delete();
        }
    }

    /** 存一張（縮到長邊 ≤1600）；回 id。失敗回 null。 */
    static String save(Context ctx, Bitmap bmp) {
        try {
            String id = "img-" + System.currentTimeMillis();
            Bitmap big = scale(bmp, MAX_EDGE);
            try (FileOutputStream out = new FileOutputStream(file(ctx, id))) {
                out.write(jpeg(big, Q));
            }
            // 縮圖：從**已經縮過**的那張再縮一次（大圖直接縮到 240 會鋸齒）。
            Bitmap small = scale(big, THUMB_EDGE);
            THUMBS.put(id, "data:image/jpeg;base64," + Base64.encodeToString(jpeg(small, 80), Base64.NO_WRAP));
            if (small != big) small.recycle();
            if (big != bmp) big.recycle();
            prune(ctx);
            return id;
        } catch (Exception e) {
            Log.i(TAG, "画像：保存できなかった " + e.getMessage());
            return null;
        }
    }

    /** 相簿／選擇器給的 URI → 存起來。先讀尺寸再決定取樣率（不然 4000 萬畫素會 OOM）。 */
    static String saveUri(Context ctx, Uri uri) {
        BitmapFactory.Options bounds = new BitmapFactory.Options();
        bounds.inJustDecodeBounds = true;
        try (InputStream in = ctx.getContentResolver().openInputStream(uri)) {
            BitmapFactory.decodeStream(in, null, bounds);
        } catch (Exception ignored) {}
        int sample = 1;
        while (bounds.outWidth / sample > MAX_EDGE * 2 || bounds.outHeight / sample > MAX_EDGE * 2) sample *= 2;
        Bitmap bmp = null;
        BitmapFactory.Options opts = new BitmapFactory.Options();
        opts.inSampleSize = sample;
        try (InputStream in = ctx.getContentResolver().openInputStream(uri)) {
            bmp = BitmapFactory.decodeStream(in, null, opts);
        } catch (Exception ignored) {}
        if (bmp == null) {
            Log.i(TAG, "画像：URI から読めなかった（権限？）");
            return null;
        }
        String id = save(ctx, bmp);
        bmp.recycle();
        return id;
    }

    /** 畫在紀錄那一列用的 `media` 物件（形狀與橋送來的產物一致）。 */
    static JSONObject mediaJson(Context ctx, String id) {
        try {
            String url = THUMBS.get(id);
            if (url == null) url = dataUrl(ctx, id);
            if (url == null) return null;
            return new JSONObject().put("kind", "image").put("url", url).put("name", "imgs/" + id + ".jpg")
                .put("bytes", file(ctx, id).length());
        } catch (Exception e) {
            return null;
        }
    }

    private static String dataUrl(Context ctx, String id) {
        File f = file(ctx, id);
        if (!f.isFile()) return null;
        Bitmap bmp = BitmapFactory.decodeFile(f.getAbsolutePath());
        if (bmp == null) return null;
        Bitmap small = scale(bmp, THUMB_EDGE);
        String url = "data:image/jpeg;base64," + Base64.encodeToString(jpeg(small, 80), Base64.NO_WRAP);
        if (small != bmp) small.recycle();
        bmp.recycle();
        THUMBS.put(id, url);
        return url;
    }
}
