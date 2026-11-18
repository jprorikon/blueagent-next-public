using System;
using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using BlueAgent.Logic;

namespace BlueAgent.Next
{
    /// <summary>
    /// 位置推送（C-14）：服务端明确授权后，把粗化座標推給 App Server（`POST /v1/me/location`）。
    ///
    /// 照現役舊殼 `LocationPush.kt` 的三個結論，不重新發明：
    /// ① **只求 COARSE**（城市級就夠）——彈窗不會出現「精確位置」那一層，他要的是天氣不是被追蹤；
    /// ② **優先用 `network` provider**（基地台／WiFi，室內也拿得到；衛星在室內常等不到）；
    /// ③ **10 分鐘一跳**，開機先要一次當下 fix（那一發才是「現在在哪」）。
    ///
    /// 失敗（沒權限／沒 provider／推不出去）**不影響任何既有功能**：橋那邊沒有座標就退回 IP 概算。
    /// 這支只是一個感測器，不取得任何工具權。
    /// </summary>
    public static class NextLocation
    {
        private const string Permission = "android.permission.ACCESS_COARSE_LOCATION";
        private const int RequestCode = 4711;

        /// <summary>最近一次成功的座標（診斷用）。</summary>
        public static string LastState { get; private set; } = "off";

        /// <summary>强制停止协程时也必须关闭系统定位；仅停止 IEnumerator 不会执行其末尾。</summary>
        public static void Stop()
        {
#if UNITY_IOS && !UNITY_EDITOR
            Input.location.Stop();
#endif
            LastState = "off";
        }

        /// <summary>
        /// 前台迴圈：每 <paramref name="intervalSeconds"/> 秒讀一次粗化座標並推給 App Server。
        /// <paramref name="alive"/>＝「開這個迴圈的那段登入還在不在」：不在就停，讀都不讀（unity-review U-3：
        /// 以前開機就無條件跑、登出也不停，舊 token 還有效時就照樣以舊帳號上傳位置）。
        /// </summary>
        public static IEnumerator Loop(NextApi api, Action<string> onState, Func<bool> alive,
                                       int intervalSeconds = 600)
        {
            if (api == null || alive == null) yield break;
            var asked = false;
            while (alive())
            {
                if (!asked)
                {
                    asked = true;
                    EnsurePermission(onState);
                }
                double lat, lon, acc;
                if (TryRead(out lat, out lon, out acc))
                {
                    Push(api, lat, lon, acc, onState, alive);
                }
                else
                {
                    State(onState, "no_fix");
                }
                yield return new WaitForSeconds(Mathf.Max(60, intervalSeconds));
            }
            Stop();
        }

        private static void State(Action<string> onState, string value)
        {
            LastState = value;
            if (onState != null) onState(value);
        }

        private static AndroidJavaObject Activity()
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            using (var player = new AndroidJavaClass("com.unity3d.player.UnityPlayer"))
            {
                return player.GetStatic<AndroidJavaObject>("currentActivity");
            }
#else
            return null;
#endif
        }

        /// <summary>沒權限就問一次（只要 COARSE）。</summary>
        private static void EnsurePermission(Action<string> onState)
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            var activity = Activity();
            if (activity == null) { State(onState, "no_activity"); return; }
            try
            {
                var granted = activity.Call<int>("checkSelfPermission", Permission);
                if (granted == 0) { State(onState, "granted"); return; }
                activity.Call("requestPermissions", new[] { Permission }, RequestCode);
                State(onState, "requested");
            }
            catch (Exception)
            {
                State(onState, "permission_error");
            }
#elif UNITY_IOS && !UNITY_EDITOR
            // 1 km 精度＋plist 預設 reduced accuracy；不要求精確定位，也不申請背景定位。
            Input.location.Start(1000f, 1000f);
            State(onState, "requested");
#else
            State(onState, "unsupported");
#endif
        }

        /// <summary>讀一次座標：先 `network`（室內也能拿）再 `gps`。</summary>
        private static bool TryRead(out double lat, out double lon, out double accuracy)
        {
            lat = lon = 0;
            accuracy = -1;
#if UNITY_ANDROID && !UNITY_EDITOR
            var activity = Activity();
            if (activity == null) return false;
            try
            {
                using (var manager = activity.Call<AndroidJavaObject>("getSystemService", "location"))
                {
                    if (manager == null) return false;
                    foreach (var provider in new[] { "network", "gps" })
                    {
                        try
                        {
                            using (var location = manager.Call<AndroidJavaObject>(
                                       "getLastKnownLocation", provider))
                            {
                                if (location == null) continue;
                                lat = location.Call<double>("getLatitude");
                                lon = location.Call<double>("getLongitude");
                                accuracy = location.Call<float>("getAccuracy");
                                if (Math.Abs(lat) > 0.0001 || Math.Abs(lon) > 0.0001) return true;
                            }
                        }
                        catch (Exception)
                        {
                            // 這個 provider 沒有就換下一個
                        }
                    }
                }
            }
            catch (Exception)
            {
                return false;
            }
#elif UNITY_IOS && !UNITY_EDITOR
            if (!Input.location.isEnabledByUser) return false;
            var status = Input.location.status;
            if (status != LocationServiceStatus.Running) return false;
            var fix = Input.location.lastData;
            lat = fix.latitude;
            lon = fix.longitude;
            accuracy = fix.horizontalAccuracy;
            return Math.Abs(lat) > 0.0001 || Math.Abs(lon) > 0.0001;
#endif
            return false;
        }

        private static void Push(NextApi api, double lat, double lon, double accuracy,
                                Action<string> onState, Func<bool> alive)
        {
            if (double.IsNaN(lat) || double.IsInfinity(lat) ||
                double.IsNaN(lon) || double.IsInfinity(lon))
            {
                State(onState, "bad_fix");
                return;
            }
            if (double.IsNaN(accuracy) || double.IsInfinity(accuracy)) accuracy = 1000.0;
            var body = new JObject()
                .Put("lat", Math.Round(lat, 2))
                .Put("lon", Math.Round(lon, 2))
                .Put("accuracy", Math.Max(1000.0, accuracy))
                .Put("device", NextConfig.LocationDeviceId);
#if UNITY_IOS && !UNITY_EDITOR
            body.Put("source", "ios");
#else
            body.Put("source", "android");
#endif
            api.PostLocation(body, reply =>
            {
                if (!alive()) return;               // 回來時已經登出／換帳號 ⇒ 不更新任何狀態
                State(onState, reply != null && reply.Ok ? "sent" : "rejected");
            });
        }
    }
}
