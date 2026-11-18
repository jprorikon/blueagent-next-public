using System.Runtime.InteropServices;

namespace BlueAgent.Spike
{
    /// <summary>
    /// iOS 的 **AVAudioSession**（C# 這一側的門面）。原生實作在 `Assets/Plugins/iOS/BlueAgentAudio.mm`。
    ///
    /// 為什麼需要它（2026-10-19 真機實測）：`Microphone.Start()` 在 iOS 上**不會報錯**，
    /// 日誌也照樣印「開麥」，但 `Microphone.GetPosition()` 永遠回 0 ⇒ 一塊音訊都拿不到
    /// （真機 `app.log` 裡 `[wake] 探針：30 s 內 0 塊（沒在餵）` 就是這個症狀）。
    /// 根因是 **AVAudioSession 沒設成 PlayAndRecord ＋ setActive**——Unity 不會幫你做，也不報錯。
    /// </summary>
    public static class AudioSession
    {
#if UNITY_IOS && !UNITY_EDITOR
        [DllImport("__Internal")] private static extern void ba_audio_session_begin();
        [DllImport("__Internal")] private static extern void ba_audio_session_end();
        [DllImport("__Internal")] private static extern void ba_audio_force_speaker();
        [DllImport("__Internal")] private static extern void ba_audio_route_info(System.Text.StringBuilder buf, int cap);

        private static bool _begun;

        /// <summary>開收音／播放前叫一次（可重複叫，內部只做一次）。</summary>
        public static void Begin()
        {
            if (_begun) return;
            _begun = true;
            try
            {
                ba_audio_session_begin();
                UnityEngine.Debug.Log("[audio] AVAudioSession：PlayAndRecord ＋ setActive 完成");
            }
            catch (System.Exception e)
            {
                UnityEngine.Debug.LogWarning("[audio] AVAudioSession 開不起來：" + e.GetType().Name + " " + e.Message);
            }
        }

        /// <summary>
        /// 回前景時**重新** setActive（繞過 <see cref="Begin"/> 的「只做一次」）。
        ///
        /// 為什麼不能直接叫 `Begin()`：`_begun` 開過一次就一直是 true（沒有人叫 `End()`）⇒
        /// `Begin()` 從第二次起整支早退。而來電／Siri／別的 app 搶過音訊後，系統會把 session
        /// 收走（inactive）⇒ 不重新 setActive，麥克風回來就一直收 0 塊。
        /// 還沒開過（`Begin()` 沒被叫過＝還沒開麥）就不碰，交給開麥那一刻。
        /// </summary>
        public static void Reactivate()
        {
            if (!_begun) return;
            try
            {
                ba_audio_session_begin();
                UnityEngine.Debug.Log("[audio] AVAudioSession：回前景重新 setActive（" + RouteInfo() + "）");
            }
            catch (System.Exception e)
            {
                UnityEngine.Debug.LogWarning("[audio] AVAudioSession 重新啟用失敗：" + e.GetType().Name + " " + e.Message);
            }
        }

        /// <summary>
        /// 播放前**再**把輸出拉到喇叭。可重複呼叫。
        ///
        /// 為什麼：Unity/FMOD 在開始輸出時會自己重設 AVAudioSession，開機時設的
        /// `overrideOutputAudioPort` 會被沖掉 ⇒ 症狀＝「明明 override 過、聲音還是從聽筒出來」
        /// （2026-10-19 真機連續兩次回報）。所以每次播之前都補一次。
        /// </summary>
        public static void ForceSpeaker()
        {
            try { ba_audio_force_speaker(); } catch (System.Exception) { }
        }

        /// <summary>當前輸出埠／類別／音量（印進 app.log 用；iOS 真機沒有 logcat）。</summary>
        public static string RouteInfo()
        {
            try
            {
                var sb = new System.Text.StringBuilder(256);
                ba_audio_route_info(sb, sb.Capacity);
                return sb.ToString();
            }
            catch (System.Exception e) { return "讀不到：" + e.GetType().Name; }
        }

        /// <summary>收工（切背景／結束時）。</summary>
        public static void End()
        {
            if (!_begun) return;
            _begun = false;
            try { ba_audio_session_end(); } catch (System.Exception) { }
        }
#else
        /// <summary>非 iOS：什麼都不做（Android／桌面各自有自己的音訊設定）。</summary>
        public static void Begin() { }
        public static void Reactivate() { }
        public static void End() { }
        public static void ForceSpeaker() { }
        public static string RouteInfo() => "非 iOS";
#endif
    }
}
