using System;
using UnityEngine;

namespace BlueAgent.Spike
{
    /// <summary>
    /// 讀 Android 的 **Intent extra**（真機調參用：不必每次重建 APK）。
    ///
    /// ⚠️ 踩過的坑：`hasExtra`／`getIntExtra`／`getFloatExtra` 是 **`Intent`** 的方法、**不是 `Activity`**
    /// 的。寫成 `activity.Call&lt;bool&gt;("hasExtra", …)` 會丟 `NoSuchMethodError`，被 try 吞掉 ⇒
    /// **extra 永遠讀不到**，而且症狀安靜得可怕（app 照跑、參數一直是預設值）。
    ///
    /// 兩條路各走各的（呼叫端同時給 intent 名與環境變數名，不猜、不自動拼）：
    /// * 真機：`adb shell am start -n &lt;pkg&gt;/com.unity3d.player.UnityPlayerGameActivity --ef mouthFixed 1.0`
    /// * 編輯器／其他平台：環境變數（名字由呼叫端指定，例如 `BA_MOUTH_FIXED`）
    /// </summary>
    public static class DeviceIntent
    {
#if UNITY_ANDROID && !UNITY_EDITOR
        /// <summary>當前 Activity 的 Intent（真機讀 extra 只能從這裡）。</summary>
        private static AndroidJavaObject Current()
        {
            using var player = new AndroidJavaClass("com.unity3d.player.UnityPlayer");
            using var activity = player.GetStatic<AndroidJavaObject>("currentActivity");
            return activity.Call<AndroidJavaObject>("getIntent");
        }
#endif

        /// <summary>有沒有指定的 float extra／環境變數（沒有／讀失敗 ⇒ false）。</summary>
        public static bool TryFloat(string intentKey, string envKey, out float value, string tag = "spike")
        {
            value = 0f;
#if UNITY_ANDROID && !UNITY_EDITOR
            try
            {
                using var intent = Current();
                if (!intent.Call<bool>("hasExtra", intentKey)) return false;
                value = intent.Call<float>("getFloatExtra", intentKey, 0f);
                return true;
            }
            catch (Exception exception)
            {
                Debug.LogWarning($"[{tag}] 讀 intent extra `{intentKey}` 失敗（{exception.Message}）");
                return false;
            }
#else
            return float.TryParse(Environment.GetEnvironmentVariable(envKey),
                                  System.Globalization.NumberStyles.Float,
                                  System.Globalization.CultureInfo.InvariantCulture, out value);
#endif
        }

        /// <summary>字串 extra／環境變數；沒有 ⇒ <paramref name="fallback"/>（橋的位址、token、要送的那句）。</summary>
        public static string String(string intentKey, string envKey, string fallback, string tag = "spike")
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            try
            {
                using var intent = Current();
                var value = intent.Call<string>("getStringExtra", intentKey);
                return string.IsNullOrEmpty(value) ? fallback : value;
            }
            catch (Exception exception)
            {
                Debug.LogWarning($"[{tag}] 讀 intent extra `{intentKey}` 失敗（{exception.Message}）");
                return fallback;
            }
#else
            var text = Environment.GetEnvironmentVariable(envKey);
            return string.IsNullOrEmpty(text) ? fallback : text;
#endif
        }

        /// <summary>int extra／環境變數；沒有或非正數 ⇒ <paramref name="fallback"/>。</summary>
        public static int Int(string intentKey, string envKey, int fallback, string tag = "spike")
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            try
            {
                using var intent = Current();
                var value = intent.Call<int>("getIntExtra", intentKey, fallback);
                return value > 0 ? value : fallback;
            }
            catch (Exception exception)
            {
                Debug.LogWarning($"[{tag}] 讀 intent extra `{intentKey}` 失敗（{exception.Message}）");
                return fallback;
            }
#else
            var text = Environment.GetEnvironmentVariable(envKey);
            return int.TryParse(text, out var parsed) && parsed > 0 ? parsed : fallback;
#endif
        }
    }
}
