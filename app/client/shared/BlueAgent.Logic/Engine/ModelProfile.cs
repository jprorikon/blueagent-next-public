using System.Collections.Generic;

namespace BlueAgent.Logic.Engine
{
    /// <summary>
    /// 一個「表現通道」→ 模型參數的對應（＝ `soullink.profile.json` 的 `parameterMap` 一項）——
    /// C# 版，對照 Kotlin `Profile.kt` 的 `ChannelSpec`。
    ///
    /// 通道是引擎的語言（headX、mouthOpen、eyeBlinkL…），參數是模型／Cubism 的語言（PARAM_ANGLE_X…）。
    /// </summary>
    public sealed class ChannelSpec
    {
        public readonly string Name;

        /// <summary>這個通道寫進哪些參數（有的通道左右各一顆）。</summary>
        public readonly IReadOnlyList<string> Targets;

        /// <summary>`set`＝值直接疊上去；`subtract`＝反向疊（眨眼：值越大眼睛越閉 ⇒ 從 1.0 減下去）。</summary>
        public readonly string Mode;

        /// <summary>通道值（-1..1 語意）乘上的倍率（headX 是 30 度、bodyX 是 12 度）。</summary>
        public readonly float Scale;

        /// <summary>這個通道所屬**參數的合法範圍**（不是通道增量的夾限）。</summary>
        public readonly float Min;

        public readonly float Max;

        /// <summary>映射後的加項（參考引擎的 `offset`）。</summary>
        public readonly float Offset;

        public ChannelSpec(
            string name, IReadOnlyList<string> targets, string mode, float scale, float min, float max, float offset = 0f)
        {
            Name = name;
            Targets = targets;
            Mode = mode;
            Scale = scale;
            Min = min;
            Max = max;
            Offset = offset;
        }

        public bool Subtract => Mode == "subtract";

        /// <summary>通道值 → 參數增量（含方向與 scale；範圍夾限留到最後對「參數值」做）。</summary>
        public float Delta(float value)
        {
            var v = EngineMath.Clamp(value, -1f, 1f) * Scale;
            return Subtract ? -v : v;
        }
    }

    /// <summary>
    /// 引擎側的模型設定 —— `soullink.profile.json` 的 C# 版，對照 Kotlin `Profile.kt` 的 `ModelProfile`。
    ///
    /// 這份檔由**離線工具**產生，是引擎與模型之間的唯一契約；移植時**照讀即可**。
    /// ⚠️ `ChannelKeys` 與 `Channels` 並存是刻意的：**鍵序是資料的一部分**（後寫覆蓋先寫、
    ///    先到先寫），所以順序用清單明講，不押 `Dictionary` 的列舉順序。
    /// </summary>
    public sealed class ModelProfile
    {
        /// <summary>profile schema 版本：&gt;=2 走 v2 映射（兩遍：先覆蓋再累加），&lt;2 走舊路徑。</summary>
        public readonly int SchemaVersion;

        public readonly string ModelId;
        public readonly string DisplayName;

        public readonly IReadOnlyList<string> ChannelKeys;
        public readonly IReadOnlyDictionary<string, ChannelSpec> Channels;
        public readonly IReadOnlyDictionary<string, bool> Capabilities;

        /// <summary>參數的中立值：每一幀都從這裡起手（不做「歸零」而是「歸中立」，眼睛才不會塌掉）。</summary>
        public readonly IReadOnlyDictionary<string, float> Neutral;

        /// <summary>每個參數的平滑時間常數（毫秒）。0＝不平滑。</summary>
        public readonly IReadOnlyDictionary<string, float> SmoothingMs;

        /// <summary>待機漂移的範圍（通道名 → [下限, 上限]）。</summary>
        public readonly IReadOnlyDictionary<string, FloatRange> IdleConfig;

        public ModelProfile(
            int schemaVersion,
            string modelId,
            string displayName,
            IReadOnlyList<string> channelKeys,
            IReadOnlyDictionary<string, ChannelSpec> channels,
            IReadOnlyDictionary<string, bool> capabilities,
            IReadOnlyDictionary<string, float> neutral,
            IReadOnlyDictionary<string, float> smoothingMs,
            IReadOnlyDictionary<string, FloatRange> idleConfig)
        {
            SchemaVersion = schemaVersion;
            ModelId = modelId;
            DisplayName = displayName;
            ChannelKeys = channelKeys;
            Channels = channels;
            Capabilities = capabilities;
            Neutral = neutral;
            SmoothingMs = smoothingMs;
            IdleConfig = idleConfig;
        }

        public ChannelSpec Channel(string name) => Channels.TryGetValue(name ?? "", out var spec) ? spec : null;

        public float NeutralOf(string paramId) => Neutral.TryGetValue(paramId ?? "", out var v) ? v : 0f;

        public float SmoothingSeconds(string paramId) =>
            (SmoothingMs.TryGetValue(paramId ?? "", out var v) ? v : 0f) / 1000f;

        public override string ToString() =>
            $"ModelProfile({DisplayName}, channels={Channels.Count}, neutral={Neutral.Count}, smoothing={SmoothingMs.Count})";

        /// <summary>讀 `soullink.profile.json`。缺欄位就退回空表——引擎不該因為 profile 精簡而爆掉。</summary>
        public static ModelProfile FromJson(string text)
        {
            var root = EngineJson.Obj(EngineJson.Parse(text));

            var channels = new Dictionary<string, ChannelSpec>();
            var channelKeys = new List<string>();
            foreach (var entry in EngineJson.Obj(root["parameterMap"]).Items)
            {
                var m = EngineJson.Obj(entry.Value);
                var single = EngineJson.Str(m["target"]);
                var many = new List<string>();
                foreach (var item in EngineJson.Arr(m["targets"]))
                {
                    var s = EngineJson.Str(item);
                    if (s.Length > 0) many.Add(s);
                }
                var targets = new List<string>();
                if (single.Length > 0) targets.Add(single);
                targets.AddRange(many);
                if (targets.Count == 0) continue;
                channels[entry.Key] = new ChannelSpec(
                    name: entry.Key,
                    targets: targets,
                    mode: EngineJson.Str(m["mode"], "set"),
                    scale: EngineJson.F(m["scale"], 1f),
                    min: EngineJson.F(m["min"], -1f),
                    max: EngineJson.F(m["max"], 1f),
                    offset: EngineJson.F(m["offset"], 0f));
                channelKeys.Add(entry.Key);
            }

            var neutral = new Dictionary<string, float>();
            foreach (var entry in EngineJson.Obj(root["neutralParams"]).Items) neutral[entry.Key] = EngineJson.F(entry.Value);

            var smoothing = new Dictionary<string, float>();
            foreach (var entry in EngineJson.Obj(root["parameterSmoothing"]).Items) smoothing[entry.Key] = EngineJson.F(entry.Value);

            var idle = new Dictionary<string, FloatRange>();
            foreach (var entry in EngineJson.Obj(root["idleConfig"]).Items)
            {
                var pair = EngineJson.Arr(entry.Value);
                if (pair.Count >= 2) idle[entry.Key] = new FloatRange(EngineJson.F(pair[0]), EngineJson.F(pair[1]));
            }

            var caps = new Dictionary<string, bool>();
            foreach (var entry in EngineJson.Obj(root["capabilities"]).Items) caps[entry.Key] = EngineJson.Bool(entry.Value);

            return new ModelProfile(
                schemaVersion: EngineMath.DoubleToIntJava(EngineJson.Num(root["schemaVersion"], 1.0)),
                modelId: EngineJson.Str(root["modelId"], "unknown"),
                displayName: EngineJson.Str(root["displayName"], "unknown"),
                channelKeys: channelKeys,
                channels: channels,
                capabilities: caps,
                neutral: neutral,
                smoothingMs: smoothing,
                idleConfig: idle);
        }
    }
}
