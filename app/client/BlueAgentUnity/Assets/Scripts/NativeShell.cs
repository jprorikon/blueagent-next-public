using System;
using BlueAgent.Logic;
using UnityEngine;

namespace BlueAgent.Screens
{
    /// <summary>
    /// **Android 原生那一側的 C# 包裝**（Java＝`Plugins/Android/blueagent-permissions.androidlib/src/main/java/
    /// dev/deepsea/blueagent/shell/Native.java`）。原 app 外殼（Kotlin）才有、Unity 沒有的那幾件：
    /// 相册格子／系統選圖／原圖落地、前景服務＋常駐通知、リマインド通知、位置推送、熄屏時的耳朵。
    ///
    /// Java → C# 一律是**事件**（JSON 字串）：<see cref="Poll"/> 在 `BridgeShell.Update` 裡拉。
    /// 編輯器／非 Android：每一支都安靜地什麼都不做（<see cref="Available"/>＝false）。
    /// </summary>
    public static class NativeShell
    {
#if UNITY_IOS && !UNITY_EDITOR
        // iOS 的選圖（`Plugins/iOS/BlueAgentPicker.mm`）：事件形狀與 Android 的 `Native.emit` 相同
        [System.Runtime.InteropServices.DllImport("__Internal")] private static extern void ba_pick_image();
        [System.Runtime.InteropServices.DllImport("__Internal")] private static extern string ba_native_poll();
        // 登入 token 的鑰匙圈（`Plugins/iOS/BlueAgentKeychain.mm`）
        [System.Runtime.InteropServices.DllImport("__Internal")] private static extern int ba_keychain_set(string token);
        [System.Runtime.InteropServices.DllImport("__Internal")] private static extern string ba_keychain_get();
        [System.Runtime.InteropServices.DllImport("__Internal")] private static extern int ba_keychain_delete();
#endif

        public const string MediaPermission33 = "android.permission.READ_MEDIA_IMAGES";
        public const string MediaPermissionOld = "android.permission.READ_EXTERNAL_STORAGE";
        public const string LocationPermission = "android.permission.ACCESS_COARSE_LOCATION";
        public const string NotificationPermission = "android.permission.POST_NOTIFICATIONS";

#if UNITY_ANDROID && !UNITY_EDITOR
        private static AndroidJavaClass _native;
        private static AndroidJavaObject _activity;
        private static int _sdk = -1;
#endif

        public static bool Available
        {
            get
            {
#if UNITY_ANDROID && !UNITY_EDITOR
                return Native() != null;
#else
                return false;
#endif
            }
        }

#if UNITY_ANDROID && !UNITY_EDITOR
        private static AndroidJavaClass Native()
        {
            if (_native != null) return _native;
            try
            {
                using var player = new AndroidJavaClass("com.unity3d.player.UnityPlayer");
                _activity = player.GetStatic<AndroidJavaObject>("currentActivity");
                _native = new AndroidJavaClass("dev.deepsea.blueagent.shell.Native");
                _native.CallStatic("init", _activity);
            }
            catch (Exception e)
            {
                Debug.LogWarning("[native] 原生那一側接不上：" + e.Message);
                _native = null;
            }
            return _native;
        }

        private static void Call(string method, params object[] args)
        {
            var native = Native();
            if (native == null) return;
            try { native.CallStatic(method, args); }
            catch (Exception e) { Debug.LogWarning($"[native] {method} 失敗：{e.Message}"); }
        }
#endif

        /// <summary>Android API 等級（非 Android＝0）。</summary>
        public static int Sdk
        {
            get
            {
#if UNITY_ANDROID && !UNITY_EDITOR
                if (_sdk < 0)
                {
                    try { using var v = new AndroidJavaClass("android.os.Build$VERSION"); _sdk = v.GetStatic<int>("SDK_INT"); }
                    catch { _sdk = 0; }
                }
                return _sdk;
#else
                return 0;
#endif
            }
        }

        /// <summary>13+ `READ_MEDIA_IMAGES`、12 以下 `READ_EXTERNAL_STORAGE`（`MainActivity.mediaPermission`）。</summary>
        public static string MediaPermission => Sdk >= 33 ? MediaPermission33 : MediaPermissionOld;

        public static void Init()
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            Native();
#if BA_SELFTEST
            Call("setSelfTest", true);           // 熄屏那段也認 `bg_inject.wav`（只在驗收包；外部評審 v2 A2）
#endif
#endif
        }

        /// <summary>拉一筆事件（沒有＝null）。</summary>
        public static string Poll()
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            var native = _native;
            if (native == null) return null;
            try { return native.CallStatic<string>("poll"); }
            catch { return null; }
#elif UNITY_IOS && !UNITY_EDITOR
            return ba_native_poll();
#else
            return null;
#endif
        }

        /// <summary>
        /// 這台機器有沒有系統的安全儲存（Android Keystore／iOS 鑰匙圈）。編輯器與其他平台＝false，
        /// 呼叫端（`NextConfig.Token`）就照舊用 PlayerPrefs。
        /// </summary>
        public static bool HasSecureStore
        {
            get
            {
#if UNITY_ANDROID && !UNITY_EDITOR
                return Native() != null;
#elif UNITY_IOS && !UNITY_EDITOR
                return true;
#else
                return false;
#endif
            }
        }

        /// <summary>存登入 token（空＝清）。回 false＝沒存進安全儲存，呼叫端要自己退回。</summary>
        public static bool SecureTokenPut(string token)
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            var native = Native();
            if (native == null) return false;
            try { return native.CallStatic<bool>(string.IsNullOrEmpty(token) ? "secureTokenClear" : "secureTokenPut",
                                                 string.IsNullOrEmpty(token) ? new object[0] : new object[] { token }); }
            catch (Exception e) { Debug.LogWarning("[native] secureTokenPut 失敗：" + e.Message); return false; }
#elif UNITY_IOS && !UNITY_EDITOR
            try { return string.IsNullOrEmpty(token) ? ba_keychain_delete() == 1 : ba_keychain_set(token) == 1; }
            catch (Exception e) { Debug.LogWarning("[native] keychain set 失敗：" + e.Message); return false; }
#else
            return false;
#endif
        }

        /// <summary>讀登入 token；沒有／讀不到＝空字串。</summary>
        public static string SecureTokenGet()
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            var native = Native();
            if (native == null) return "";
            try { return native.CallStatic<string>("secureTokenGet") ?? ""; }
            catch (Exception e) { Debug.LogWarning("[native] secureTokenGet 失敗：" + e.Message); return ""; }
#elif UNITY_IOS && !UNITY_EDITOR
            try { return ba_keychain_get() ?? ""; }
            catch (Exception e) { Debug.LogWarning("[native] keychain get 失敗：" + e.Message); return ""; }
#else
            return "";
#endif
        }

        public static void Configure(string url, string token, string device)
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            Call("configure", url ?? "", token ?? "", device ?? "");
#endif
        }

        public static void AlbumPage(int offset, int count)
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            Call("albumPage", offset, count);
#endif
        }

        public static void SaveAlbumImage(string id)
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            Call("saveAlbumImage", id ?? "");
#endif
        }

        public static void PickImage()
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            if (Native() != null) Call("pickImage", _activity);
#elif UNITY_IOS && !UNITY_EDITOR
            ba_pick_image();                     // 系統照片挑選器（PHPicker），不必相簿權限
#endif
        }

        public static void StartShellService(string note)
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            if (Native() != null) Call("startShellService", _activity, note ?? "");
#endif
            Debug.Log("[native] 前景服務：" + note);
        }

        public static void NotifyReminder(string title, string text)
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            Call("notifyReminder", title ?? "", text ?? "");
#endif
        }

        /// <summary>
        /// 背景系統推送（B.App.8，Java `NoticeListen`）：App Server 位址、token、自己的帳號 id、
        /// 暫停那一刻的事件游標。`on=false` ⇒ 停（登出）。編輯器／非 Android：什麼都不做。
        /// </summary>
        public static void ConfigureNotices(string url, string token, string account, string lastEventId, bool on)
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            Call("configureNotices", url ?? "", token ?? "", account ?? "", lastEventId ?? "", on);
#endif
        }

        public static bool StartLocation()
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            var native = Native();
            if (native == null) return false;
            try { return native.CallStatic<bool>("startLocation", _activity); }
            catch (Exception e) { Debug.LogWarning("[native] startLocation 失敗：" + e.Message); return false; }
#else
            return false;
#endif
        }

        public static void SetEars(bool on, string session, string soul, string mode, bool tts)
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            Call("setEars", on, session ?? "", soul ?? "", mode ?? "", tts);
#endif
        }

        /// <summary>收音設定（`audio_source`／`platform_effects`／`kws_barge_in`）——熄屏耳朵也照這一份開麥。</summary>
        public static void SetMic(string source, bool effects, bool bargeIn)
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            Call("setMic", source ?? "", effects, bargeIn);
#endif
        }

        /// <summary>熄屏那段的喚醒網設定（`BgWake.java`；與前景 `WakeNet` 同一組）。只是記住，下次熄屏才生效。</summary>
        public static void SetWake(bool on, bool usable, string modelsDir, string character, SettingsStore s)
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            Call("setWake", on, usable, modelsDir ?? "", character ?? "",
                s.KwsThreshold, s.KwsConsecutive, s.KwsDecideEvery, s.KwsCooldownMs,
                s.SpeakerGate, s.SpeakerThreshold, s.GateRule ?? "", s.GateMinEvalMs, s.GateDelayMs,
                s.SpkSherpa ?? "", s.SpkSherpaThreshold, s.SpkSherpaProvider ?? "cpu", s.SpkSherpaThreads);
#endif
        }

        /// <summary>帶通（`voice_filter`／高通／低通）——熄屏那段照前景 `MicCapture` 濾。</summary>
        public static void SetBand(bool on, double highPassHz, double lowPassHz)
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            Call("setBand", on, highPassHz, lowPassHz);
#endif
        }

        // ── 前景收音（`MicRecord.java`：AudioRecord＋平台 NS／AGC／AEC）──────────────────────

        /// <summary>開麥；回 `MicRecord.start` 的 JSON（`ok`／`source`／`available`／`attached`／`aec`／`why`）；非 Android＝null。</summary>
        public static string MicStart(string source, bool effects)
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            try
            {
                using var mic = new AndroidJavaClass("dev.deepsea.blueagent.shell.MicRecord");
                return mic.CallStatic<string>("start", source ?? "", effects);
            }
            catch (Exception e)
            {
                Debug.LogWarning("[native] MicRecord.start 失敗：" + e.Message);
                return "{\"ok\":false,\"why\":\"" + e.Message.Replace("\"", "'") + "\"}";
            }
#else
            return null;
#endif
        }

#if UNITY_ANDROID && !UNITY_EDITOR
        private static AndroidJavaClass _mic;
#endif

        /// <summary>上次之後錄到的樣本（沒有＝空）。</summary>
        public static short[] MicDrain()
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            try
            {
                _mic ??= new AndroidJavaClass("dev.deepsea.blueagent.shell.MicRecord");
                return _mic.CallStatic<short[]>("drain") ?? Array.Empty<short>();
            }
            catch { return Array.Empty<short>(); }
#else
            return Array.Empty<short>();
#endif
        }

        public static void MicStop()
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            try
            {
                using var mic = new AndroidJavaClass("dev.deepsea.blueagent.shell.MicRecord");
                mic.CallStatic("stop");
            }
            catch (Exception e) { Debug.LogWarning("[native] MicRecord.stop 失敗：" + e.Message); }
#endif
        }

        public static void SetVolume(float volume)
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            Call("setVolume", volume);
#endif
        }

        /// <summary>背景に回る：鳴っていた一句の殘り＋待ち行列（`paths` は改行區切り）を原生で鳴らし續ける。</summary>
        public static void HandOverSpeech(string paths, int startMs)
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            Call("handOverSpeech", paths ?? "", startMs);
#endif
        }

        /// <summary>前景に戻った：原生で鳴り終わらなかった分 `{"pos":ms,"files":[...]}`（無ければ空）。</summary>
        public static string TakeBackSpeech()
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            var native = Native();
            if (native == null) return "";
            try { return native.CallStatic<string>("takeBackSpeech") ?? ""; }
            catch (Exception e) { Debug.LogWarning("[native] takeBackSpeech 失敗：" + e.Message); return ""; }
#else
            return "";
#endif
        }

        /// <summary>熄屏那段的應答音（`BgEars`）：`dir`＝匯出好的 wav 資料夾；`on=false` ⇒ 不播。</summary>
        public static void SetAck(bool on, string dir)
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            Call("setAck", on, dir ?? "");
#endif
        }

        // ── 權限（`UnityEngine.Android.Permission`；一次只問一顆，問完才問下一顆）──────────────

        public static bool Has(string permission)
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            return UnityEngine.Android.Permission.HasUserAuthorizedPermission(permission);
#else
            return true;
#endif
        }

        /// <summary>要一顆權限；已有＝立刻回 true。`done` 在主執行緒上叫。</summary>
        public static void Request(string permission, Action<bool> done)
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            if (Has(permission)) { done?.Invoke(true); return; }
            var callbacks = new UnityEngine.Android.PermissionCallbacks();
            callbacks.PermissionGranted += _ => { Debug.Log("[native] 權限給了：" + permission); done?.Invoke(true); };
            callbacks.PermissionDenied += _ => { Debug.Log("[native] 權限被拒：" + permission); done?.Invoke(false); };
#pragma warning disable CS0618
            callbacks.PermissionDeniedAndDontAskAgain += _ => { Debug.Log("[native] 權限被拒（不再問）：" + permission); done?.Invoke(false); };
#pragma warning restore CS0618
            UnityEngine.Android.Permission.RequestUserPermission(permission, callbacks);
#else
            done?.Invoke(true);
#endif
        }
    }
}
