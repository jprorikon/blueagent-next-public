using System.Collections.Generic;

namespace BlueAgent.Logic.Engine
{
    /// <summary>
    /// 這個模型「有哪些表現能力」—— C# 版，對照 Kotlin `profile/CapabilityDetector.kt`。
    ///
    /// 為什麼需要它：同一套表現引擎要餵不同模型（有的沒眉毛、有的沒腮紅）。待機小動作與手勢
    /// 在生成前會先問能力表，做不到的動作就不排程 —— 這樣換模型時不會出現「參數寫了但模型沒這顆」
    /// 的靜默失敗。
    /// </summary>
    public sealed class ModelCapabilities
    {
        public readonly bool HeadControl;
        public readonly bool BodyControl;
        public readonly bool EyeBlink;
        public readonly bool EyeSmile;
        public readonly bool GazeControl;
        public readonly bool MouthOpen;
        public readonly bool MouthSmile;
        public readonly bool BrowControl;
        public readonly bool Blush;
        public readonly bool Tear;
        public readonly bool Sweat;
        public readonly bool Breath;

        public ModelCapabilities(
            bool headControl = false,
            bool bodyControl = false,
            bool eyeBlink = false,
            bool eyeSmile = false,
            bool gazeControl = false,
            bool mouthOpen = false,
            bool mouthSmile = false,
            bool browControl = false,
            bool blush = false,
            bool tear = false,
            bool sweat = false,
            bool breath = false)
        {
            HeadControl = headControl;
            BodyControl = bodyControl;
            EyeBlink = eyeBlink;
            EyeSmile = eyeSmile;
            GazeControl = gazeControl;
            MouthOpen = mouthOpen;
            MouthSmile = mouthSmile;
            BrowControl = browControl;
            Blush = blush;
            Tear = tear;
            Sweat = sweat;
            Breath = breath;
        }

        public bool Any =>
            HeadControl || BodyControl || EyeBlink || EyeSmile || GazeControl || MouthOpen ||
            MouthSmile || BrowControl || Blush || Tear || Sweat || Breath;

        /// <summary>從 profile 的 `capabilities` 區塊（字串→布林）讀。</summary>
        public static ModelCapabilities FromMap(IReadOnlyDictionary<string, bool> map)
        {
            bool Get(string key) => map != null && map.TryGetValue(key, out var v) && v;
            return new ModelCapabilities(
                headControl: Get("headControl"),
                bodyControl: Get("bodyControl"),
                eyeBlink: Get("eyeBlink"),
                eyeSmile: Get("eyeSmile"),
                gazeControl: Get("gazeControl"),
                mouthOpen: Get("mouthOpen"),
                mouthSmile: Get("mouthSmile"),
                browControl: Get("browControl"),
                blush: Get("blush"),
                tear: Get("tear"),
                sweat: Get("sweat"),
                breath: Get("breath"));
        }

        /// <summary>`withConservativeFallback`：全部都沒能力時，退回「常見模型都有的那幾項」。</summary>
        public ModelCapabilities WithConservativeFallback()
        {
            if (Any) return this;
            return new ModelCapabilities(
                headControl: true,
                bodyControl: true,
                eyeBlink: true,
                gazeControl: true,
                browControl: true);
        }
    }

    /// <summary>能力偵測（與 <see cref="ModelCapabilities"/> 同檔，對應 Kotlin 同一個檔案）。</summary>
    public static class CapabilityDetector
    {
        /// <summary>`detectCapabilities`：從通道表推。</summary>
        public static ModelCapabilities DetectCapabilities(ModelProfile profile)
        {
            bool Has(params string[] names)
            {
                foreach (var name in names)
                {
                    if (profile.Channels.ContainsKey(name)) return true;
                }
                return false;
            }

            return new ModelCapabilities(
                headControl: Has("headX", "headY", "headZ"),
                bodyControl: Has("bodyX", "bodyY", "bodyZ"),
                eyeBlink: Has("eyeBlinkL", "eyeBlinkR"),
                eyeSmile: Has("eyeSmile"),
                gazeControl: Has("gazeX", "gazeY"),
                mouthOpen: Has("mouthOpen"),
                mouthSmile: Has("mouthSmile"),
                browControl: Has("browInnerUp", "browOuterUp", "browDown"),
                blush: Has("blush"),
                tear: Has("tear"),
                sweat: Has("sweat"),
                breath: Has("breath"));
        }

        /// <summary>`withConservativeFallback`（模組層函式版，與 <see cref="ModelCapabilities.WithConservativeFallback"/> 同語意）。</summary>
        public static ModelCapabilities WithConservativeFallback(ModelCapabilities capabilities) =>
            capabilities.WithConservativeFallback();

        /// <summary>
        /// 取模型實際能力 —— **照上游語意**：
        ///   ① profile 有寫 `capabilities` ⇒ 直接用（即使全是 false，那是模型真的沒有）；
        ///   ② profile 沒寫 ⇒ 從通道表推測；
        ///   ③ 推測結果「全 false」 ⇒ 退保守值，免得「profile 沒寫能力表」變成「角色完全不會動」。
        /// </summary>
        public static ModelCapabilities ResolveCapabilities(ModelProfile profile)
        {
            if (profile.Capabilities.Count > 0) return ModelCapabilities.FromMap(profile.Capabilities);
            return DetectCapabilities(profile).WithConservativeFallback();
        }
    }
}
