package dev.deepsea.blueagent.shell;

import android.content.ContentUris;
import android.content.Context;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.Uri;
import android.provider.MediaStore;
import android.util.Base64;
import android.util.Log;
import android.util.Size;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;

/**
 * 我們自己的相册格子——原 app `AlbumStore.kt` 的逐條移植。
 *
 * 一次回一頁（時間倒序）：
 * <pre>{"items":[{"id":"1234","thumb":"data:image/jpeg;base64,…"}],"offset":0,"count":30,"total":812}</pre>
 * 縮圖走 `ContentResolver.loadThumbnail`（API 29+；更舊的機器自己解一張小圖）；算過的按 id 快取。
 * 原圖不進來——送出去的時候才由 {@link Native#saveAlbumImage} 按 id 讀原圖。
 */
final class AlbumStore {
    private static final String TAG = Native.TAG;
    private static final int THUMB_PX = 200;
    private static final int MAX_PAGE = 60;

    private static final LinkedHashMap<String, String> THUMBS = new LinkedHashMap<>();

    private AlbumStore() {}

    /** 一頁相册。沒有權限／沒有相册 ⇒ items 空的（呼叫端照畫空狀態）。 */
    static String page(Context ctx, int offset, int count) throws Exception {
        JSONArray items = new JSONArray();
        int limit = Math.max(1, Math.min(MAX_PAGE, count));
        List<String> ids = ids(ctx);
        int from = Math.max(0, offset);
        int added = 0;
        for (int i = from; i < ids.size() && i < from + limit; i++) {
            String id = ids.get(i);
            String thumb = THUMBS.get(id);
            if (thumb == null) thumb = thumbOf(ctx, id);
            if (thumb == null) continue;
            THUMBS.put(id, thumb);
            items.put(new JSONObject().put("id", id).put("thumb", thumb));
            added++;
        }
        return new JSONObject().put("items", items).put("offset", offset)
            .put("count", added).put("total", ids.size()).toString();
    }

    /** 相册裡所有圖的 id，新的在前。查詢失敗（權限被收回等）⇒ 空清單。 */
    private static List<String> ids(Context ctx) {
        ArrayList<String> out = new ArrayList<>();
        try (Cursor c = ctx.getContentResolver().query(
                MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
                new String[] { MediaStore.Images.Media._ID },
                null, null,
                MediaStore.Images.Media.DATE_ADDED + " DESC")) {
            if (c != null) {
                int col = c.getColumnIndexOrThrow(MediaStore.Images.Media._ID);
                while (c.moveToNext()) out.add(Long.toString(c.getLong(col)));
            }
        } catch (Exception e) {
            Log.i(TAG, "相册：查不到（権限？）" + e.getMessage());
        }
        return out;
    }

    private static String thumbOf(Context ctx, String id) {
        try {
            Uri uri = uriOf(Long.parseLong(id));
            Bitmap bmp;
            if (android.os.Build.VERSION.SDK_INT >= 29) {
                bmp = ctx.getContentResolver().loadThumbnail(uri, new Size(THUMB_PX, THUMB_PX), null);
            } else {
                bmp = decodeScaled(ctx, uri, THUMB_PX);
            }
            if (bmp == null) return null;
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            bmp.compress(Bitmap.CompressFormat.JPEG, 78, out);
            bmp.recycle();
            return "data:image/jpeg;base64," + Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP);
        } catch (Exception e) {
            Log.i(TAG, "相册：縮圖失敗 id=" + id + " " + e.getMessage());
            return null;
        }
    }

    /** id → 相册那條 content URI。 */
    static Uri uriOf(long id) {
        return ContentUris.withAppendedId(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, id);
    }

    /** 兜底：有些 ROM 的 `loadThumbnail` 會回 null ⇒ 自己解一張小圖。 */
    static Bitmap decodeScaled(Context ctx, Uri uri, int maxEdge) {
        try {
            BitmapFactory.Options bounds = new BitmapFactory.Options();
            bounds.inJustDecodeBounds = true;
            try (InputStream in = ctx.getContentResolver().openInputStream(uri)) {
                BitmapFactory.decodeStream(in, null, bounds);
            }
            int sample = 1;
            while (bounds.outWidth / sample > maxEdge * 2 || bounds.outHeight / sample > maxEdge * 2) sample *= 2;
            BitmapFactory.Options opts = new BitmapFactory.Options();
            opts.inSampleSize = sample;
            try (InputStream in = ctx.getContentResolver().openInputStream(uri)) {
                return BitmapFactory.decodeStream(in, null, opts);
            }
        } catch (Exception e) {
            return null;
        }
    }
}
