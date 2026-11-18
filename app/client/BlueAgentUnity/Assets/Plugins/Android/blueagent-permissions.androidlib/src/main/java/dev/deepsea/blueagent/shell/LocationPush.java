package dev.deepsea.blueagent.shell;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Bundle;
import android.os.Looper;
import android.util.Log;

import org.json.JSONObject;

/**
 * 位置推送（原 app `LocationPush.kt`）：**有座標就推給橋**（`POST /v1/loc`），天氣怎麼算在橋那邊。
 *
 * 照原版的三個選擇：只要 COARSE（城市級就夠）、`NETWORK_PROVIDER` 優先（室內拿得到、省電）、
 * 10 分鐘一跳（開 app 先要一次當下的 fix）。失敗都不影響任何既有功能（橋會退回 IP 概算）。
 */
final class LocationPush {
    private static final long INTERVAL_MS = 10 * 60 * 1000L;

    private final Context context;
    private final LocationManager lm;
    private LocationListener listener;

    LocationPush(Context context) {
        this.context = context;
        this.lm = (LocationManager) context.getSystemService(Context.LOCATION_SERVICE);
    }

    boolean hasPermission() {
        return context.checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION)
            == PackageManager.PERMISSION_GRANTED;
    }

    private String provider() {
        if (lm == null) return null;
        try { if (lm.isProviderEnabled(LocationManager.NETWORK_PROVIDER)) return LocationManager.NETWORK_PROVIDER; }
        catch (Exception ignored) {}
        try { if (lm.isProviderEnabled(LocationManager.GPS_PROVIDER)) return LocationManager.GPS_PROVIDER; }
        catch (Exception ignored) {}
        return null;
    }

    private void report(Location loc) {
        if (loc == null) return;
        final double lat = loc.getLatitude(), lon = loc.getLongitude();
        final float acc = loc.getAccuracy();
        Native.WORKER.execute(() -> {
            boolean ok = false;
            try {
                JSONObject body = new JSONObject().put("lat", lat).put("lon", lon);
                if (acc > 0f) body.put("acc", (double) acc);
                ok = Http.postJson("/v1/loc", body.toString()) / 100 == 2;
            } catch (Exception e) {
                Log.i(Native.TAG, "位置：送出失敗 " + e.getMessage());
            }
            Log.i(Native.TAG, ok ? "位置：座標を橋へ送った" : "位置：座標を送れなかった（天気は IP 概算に落ちる）");
        });
    }

    /** 開始（冪等）。沒有權限＝回 false（呼叫端負責去問權限）。要在有 Looper 的執行緒叫。 */
    @SuppressWarnings("MissingPermission")
    boolean start() {
        if (lm == null || !hasPermission()) return false;
        if (listener != null) return true;
        String name = provider();
        if (name == null) {
            Log.i(Native.TAG, "位置：provider が無い");
            return false;
        }
        // ① 當下這一發（開 app 就送，讓橋手上有「現在」的座標）。
        try {
            if (android.os.Build.VERSION.SDK_INT >= 30) {
                lm.getCurrentLocation(name, null, context.getMainExecutor(), this::report);
            } else {
                report(lm.getLastKnownLocation(name));
            }
        } catch (Exception e) {
            Log.i(Native.TAG, "位置：getCurrentLocation 失敗 " + e.getMessage());
        }
        // ② 之後的週期更新（10 分鐘；距離門檻 0 ⇒ 只由時間決定）。
        LocationListener l = new LocationListener() {
            @Override public void onLocationChanged(Location location) { report(location); }
            @Override public void onProviderEnabled(String provider) {}
            @Override public void onProviderDisabled(String provider) {}
            @Override public void onStatusChanged(String provider, int status, Bundle extras) {}
        };
        try {
            lm.requestLocationUpdates(name, INTERVAL_MS, 0f, l, Looper.getMainLooper());
        } catch (Exception e) {
            Log.i(Native.TAG, "位置：requestLocationUpdates 失敗（provider=" + name + "）");
            return false;
        }
        listener = l;
        Log.i(Native.TAG, "位置：開始（provider=" + name + ", " + (INTERVAL_MS / 60000) + " 分ごと）");
        return true;
    }
}
