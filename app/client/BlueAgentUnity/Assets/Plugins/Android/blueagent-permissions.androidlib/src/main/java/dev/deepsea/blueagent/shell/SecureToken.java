package dev.deepsea.blueagent.shell;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import android.util.Log;

import java.nio.charset.StandardCharsets;
import java.security.KeyStore;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/**
 * 登入 token 存 **Android Keystore**（PENDING B.App.4，2026-11-17）。
 *
 * 以前 C# 用 PlayerPrefs 明文存（`shared_prefs/*.xml`，root／備份／偵錯版 run-as 都讀得到）。
 * 現在：Keystore 裡一把不可匯出的 AES-256-GCM 金鑰（{@link #ALIAS}），token 加密後的
 * `IV || 密文` base64 放在自己的 SharedPreferences（{@link #PREFS}）。金鑰離不開這台機器的 Keystore，
 * 檔案被拷走也解不開。
 *
 * 失敗一律回空字串／false、不丟例外：C# 那邊（`NextSecureStore`）看到 false 就退回 PlayerPrefs，
 * 不能因為某台機器 Keystore 壞掉就登入不了。
 */
public final class SecureToken {
    private static final String ALIAS = "blueagent_token_v1";
    private static final String PREFS = "blueagent_secure";
    private static final String KEY = "token";
    private static final int IV_BYTES = 12;
    private static final int TAG_BITS = 128;

    private SecureToken() {}

    private static SecretKey key() throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore");
        store.load(null);
        KeyStore.Entry entry = store.getEntry(ALIAS, null);
        if (entry instanceof KeyStore.SecretKeyEntry) return ((KeyStore.SecretKeyEntry) entry).getSecretKey();
        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build());
        return generator.generateKey();
    }

    private static SharedPreferences prefs(Context context) {
        return context.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    /** 存一把 token（空字串＝清掉）。回 true＝真的進了 Keystore。 */
    public static boolean put(Context context, String token) {
        if (context == null) return false;
        if (token == null || token.isEmpty()) return clear(context);
        try {
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.ENCRYPT_MODE, key());
            byte[] iv = cipher.getIV();
            byte[] body = cipher.doFinal(token.getBytes(StandardCharsets.UTF_8));
            byte[] packed = new byte[iv.length + body.length];
            System.arraycopy(iv, 0, packed, 0, iv.length);
            System.arraycopy(body, 0, packed, iv.length, body.length);
            return prefs(context).edit().putString(KEY, Base64.encodeToString(packed, Base64.NO_WRAP)).commit();
        } catch (Exception ex) {
            Log.w(Native.TAG, "SecureToken.put：Keystore 用不了 " + ex.getClass().getSimpleName());
            return false;
        }
    }

    /** 讀 token；沒有或解不開＝空字串（解不開時順手清掉壞資料）。 */
    public static String get(Context context) {
        if (context == null) return "";
        String raw = prefs(context).getString(KEY, "");
        if (raw == null || raw.isEmpty()) return "";
        try {
            byte[] packed = Base64.decode(raw, Base64.NO_WRAP);
            if (packed.length <= IV_BYTES) throw new IllegalStateException("short");
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(TAG_BITS, packed, 0, IV_BYTES));
            byte[] plain = cipher.doFinal(packed, IV_BYTES, packed.length - IV_BYTES);
            return new String(plain, StandardCharsets.UTF_8);
        } catch (Exception ex) {
            // 金鑰被系統清掉（例如解除螢幕鎖、還原備份到新機）⇒ 舊密文永遠解不開，留著也沒用。
            Log.w(Native.TAG, "SecureToken.get：解不開，清掉 " + ex.getClass().getSimpleName());
            prefs(context).edit().remove(KEY).commit();
            return "";
        }
    }

    public static boolean clear(Context context) {
        if (context == null) return false;
        return prefs(context).edit().remove(KEY).commit();
    }
}
