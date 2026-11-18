package dev.deepsea.blueagent.shell;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * 直接打橋（只給 Unity 停住的那幾段用：位置推送、熄屏耳朵）。標頭與 C# `BridgeProtocol.Headers` 相同：
 * `Authorization: Bearer <token>`、`X-Device: <端末鍵>`。
 * ⚠️ 橋是區網的純 HTTP ⇒ manifest 要 `usesCleartextTraffic`（Unity 自己的網路不吃這條，Java 這條吃）。
 */
final class Http {
    private Http() {}

    static HttpURLConnection open(String pathAndQuery, String method) throws Exception {
        String base = Native.bridgeUrl;
        if (base == null || base.isEmpty()) throw new IllegalStateException("橋還沒設定");
        while (base.endsWith("/")) base = base.substring(0, base.length() - 1);
        HttpURLConnection c = (HttpURLConnection) new URL(base + pathAndQuery).openConnection();
        c.setRequestMethod(method);
        c.setConnectTimeout(8000);
        c.setRequestProperty("Authorization", "Bearer " + Native.bridgeToken);
        c.setRequestProperty("X-Device", Native.deviceKey);
        return c;
    }

    /** POST 一包；回 HTTP code（回應本體讀掉丟棄）。 */
    static int postJson(String path, String json) throws Exception {
        HttpURLConnection c = open(path, "POST");
        try {
            c.setReadTimeout(8000);
            c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            write(c, json.getBytes(StandardCharsets.UTF_8));
            int code = c.getResponseCode();
            read(code / 100 == 2 ? c.getInputStream() : c.getErrorStream());
            return code;
        } finally {
            c.disconnect();
        }
    }

    /** POST 位元組（`application/octet-stream`）；回 {code, body}。 */
    static Object[] postBytes(String pathAndQuery, byte[] body) throws Exception {
        HttpURLConnection c = open(pathAndQuery, "POST");
        try {
            c.setReadTimeout(20000);
            c.setRequestProperty("Content-Type", "application/octet-stream");
            write(c, body);
            int code = c.getResponseCode();
            String text = read(code / 100 == 2 ? c.getInputStream() : c.getErrorStream());
            return new Object[] { code, text };
        } finally {
            c.disconnect();
        }
    }

    private static void write(HttpURLConnection c, byte[] body) throws Exception {
        c.setDoOutput(true);
        c.setFixedLengthStreamingMode(body.length);
        try (OutputStream out = c.getOutputStream()) {
            out.write(body);
        }
    }

    private static String read(InputStream in) throws Exception {
        if (in == null) return "";
        try (InputStream s = in) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            while ((n = s.read(buf)) > 0) out.write(buf, 0, n);
            return new String(out.toByteArray(), StandardCharsets.UTF_8);
        }
    }
}
