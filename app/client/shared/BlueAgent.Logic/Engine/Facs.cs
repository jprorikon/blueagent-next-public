using System.Collections.Generic;

namespace BlueAgent.Logic.Engine
{
    /// <summary>
    /// FACS 表情層的鍵與算術 —— C# 版，對照 Kotlin `dev.deepsea.live2d.engine.facs`（`Facs.kt`）。
    ///
    /// 為什麼用 `Dictionary&lt;string, float&gt;` 而不是 24 個欄位的 struct：
    /// 參考引擎的型別就是 `Partial&lt;Record&lt;FACSKey, number&gt;&gt;`（**允許鍵缺席**），
    /// 而「缺席」與「值為 0」在 <see cref="ClampState"/> 與各層混合規則裡是**不同**的意思
    /// （缺席＝這一層不管這顆鍵）。⇒ 型別必須容得下缺席，否則移植時會把語意做丟。
    ///
    /// ⚠️ 順序：Kotlin 用的是 `LinkedHashMap`（插入序）。這裡的字典列舉順序**只影響輸出 JSON 的可讀性**，
    ///    不參與任何數值計算（混合器是照 <see cref="Keys"/> 這個固定順序走的）——所以用一般字典即可。
    /// </summary>
    public static class Facs
    {
        /// <summary>鍵的順序照 Kotlin 的 `createDefaultFACSState()`（順序會影響 clamp 的求值順序，保持一致）。</summary>
        public static readonly IReadOnlyList<string> Keys = new[]
        {
            "browInnerUp", "browOuterUp", "browDown",
            "eyeOpen", "eyeSmile", "eyeSquint", "eyeBlinkL", "eyeBlinkR",
            "mouthSmile", "mouthFrown", "mouthOpen", "mouthPucker",
            "gazeX", "gazeY",
            "headX", "headY", "headZ",
            "bodyX", "bodyY", "bodyZ",
            "blush", "tear", "sweat",
            "breath",
        };

        /// <summary>有方向性的鍵（範圍 -1..1）。</summary>
        public static readonly HashSet<string> Directional = new HashSet<string>
        {
            "gazeX", "gazeY", "headX", "headY", "headZ", "bodyX", "bodyY", "bodyZ",
        };

        /// <summary>混合時「累加」的鍵（`mixer/PriorityRules.ts`）。</summary>
        public static readonly HashSet<string> Additive = new HashSet<string>
        {
            "headX", "headY", "headZ", "bodyX", "bodyY", "bodyZ", "breath", "eyeBlinkL", "eyeBlinkR",
        };

        /// <summary>混合時「取大」的鍵（`mixer/PriorityRules.ts`）。</summary>
        public static readonly HashSet<string> Max = new HashSet<string>
        {
            "mouthOpen", "blush", "tear", "sweat",
        };

        /// <summary>
        /// `createDefaultFACSState()`：**不是全 0** —— eyeOpen＝1、mouthSmile＝0.04、breath＝0.5。
        /// 混合的起點就是它（漏掉這幾顆，輸出的整片基準就錯了）。
        /// </summary>
        public static Dictionary<string, float> CreateDefaultState()
        {
            return new Dictionary<string, float>
            {
                { "browInnerUp", 0f }, { "browOuterUp", 0f }, { "browDown", 0f },
                { "eyeOpen", 1f }, { "eyeSmile", 0f }, { "eyeSquint", 0f }, { "eyeBlinkL", 0f }, { "eyeBlinkR", 0f },
                { "mouthSmile", 0.04f }, { "mouthFrown", 0f }, { "mouthOpen", 0f }, { "mouthPucker", 0f },
                { "gazeX", 0f }, { "gazeY", 0f },
                { "headX", 0f }, { "headY", 0f }, { "headZ", 0f },
                { "bodyX", 0f }, { "bodyY", 0f }, { "bodyZ", 0f },
                { "blush", 0f }, { "tear", 0f }, { "sweat", 0f },
                { "breath", 0.5f },
            };
        }

        /// <summary>`rangeFor`：方向鍵 -1..1；`eyeOpen` 0..1.25；其餘 0..1。</summary>
        public static void RangeFor(string key, out float min, out float max)
        {
            if (Directional.Contains(key)) { min = -1f; max = 1f; return; }
            if (key == "eyeOpen") { min = 0f; max = 1.25f; return; }
            min = 0f; max = 1f;
        }

        public static float ClampValue(string key, float value)
        {
            RangeFor(key, out var min, out var max);
            return EngineMath.Clamp(value, min, max);
        }

        /// <summary>只夾「有出現」的鍵（缺席的鍵保持缺席）——與 `clampFACSState` 同義。</summary>
        public static Dictionary<string, float> ClampState(IDictionary<string, float> state)
        {
            var outp = new Dictionary<string, float>(state.Count);
            foreach (var kv in state) outp[kv.Key] = ClampValue(kv.Key, kv.Value);
            return outp;
        }

        /// <summary>
        /// `add`：`(base[k] ?: 0) + v * weight` 之後**逐鍵夾限**。
        /// ⚠️ overlay 的鍵若不在 base 裡，是從 0 起算（不是略過）。
        /// </summary>
        public static Dictionary<string, float> Add(
            IDictionary<string, float> baseState, IDictionary<string, float> overlay, float weight = 1f)
        {
            var outp = new Dictionary<string, float>(baseState);
            foreach (var kv in overlay)
            {
                outp.TryGetValue(kv.Key, out var b);
                outp[kv.Key] = b + kv.Value * weight;
            }
            return ClampState(outp);
        }

        /// <summary>`scale`：逐鍵乘完之後夾限。</summary>
        public static Dictionary<string, float> Scale(IReadOnlyDictionary<string, float> state, float scale)
        {
            var outp = new Dictionary<string, float>(state.Count);
            foreach (var kv in state) outp[kv.Key] = kv.Value * scale;
            return ClampState(outp);
        }

        /// <summary>`mergeFACS`：覆蓋式合併（overlay 贏）後夾限。</summary>
        public static Dictionary<string, float> Merge(
            IDictionary<string, float> baseState, IDictionary<string, float> overlay)
        {
            var outp = new Dictionary<string, float>(baseState);
            foreach (var kv in overlay) outp[kv.Key] = kv.Value;
            return ClampState(outp);
        }

        /// <summary>
        /// `scaleFACSFromNeutral`：把一組表情「從中立值往目標插值 scale 倍」。
        ///
        /// ⚠️ 是 `lerp(中立值, 目標值, scale)`，**不是** `目標值 * scale` —— 兩者在
        ///    `eyeOpen`（中立 = 1）與 `mouthSmile`（中立 = 0.04）上差很多。
        /// </summary>
        public static Dictionary<string, float> ScaleFromNeutral(IReadOnlyDictionary<string, float> state, float scale)
        {
            var neutral = CreateDefaultState();
            var outp = new Dictionary<string, float>(state.Count);
            foreach (var kv in state)
            {
                neutral.TryGetValue(kv.Key, out var n);
                outp[kv.Key] = EngineMath.Lerp(n, kv.Value, scale);
            }
            return ClampState(outp);
        }

        /// <summary>
        /// `interpolateFACS`：兩組表情按 t 插值（缺席的鍵當 0）。
        /// 鍵的集合＝from ∪ to（Kotlin 是 `LinkedHashSet(from.keys)` 再 `addAll(to.keys)`）。
        /// </summary>
        public static Dictionary<string, float> Interpolate(
            IDictionary<string, float> from, IDictionary<string, float> to, float t)
        {
            var outp = new Dictionary<string, float>(from.Count + to.Count);
            foreach (var kv in from)
            {
                to.TryGetValue(kv.Key, out var tv);
                outp[kv.Key] = EngineMath.Lerp(kv.Value, tv, t);
            }
            foreach (var kv in to)
            {
                if (outp.ContainsKey(kv.Key)) continue;      // from 那一側已經算過
                from.TryGetValue(kv.Key, out var fv);
                outp[kv.Key] = EngineMath.Lerp(fv, kv.Value, t);
            }
            return ClampState(outp);
        }

        /// <summary>`normalizeFACS`：補齊全部鍵（缺席沿用預設）後再逐鍵夾限（回傳完整狀態）。</summary>
        public static Dictionary<string, float> Normalize(IDictionary<string, float> partial)
        {
            var outp = CreateDefaultState();
            foreach (var k in Keys)
            {
                var v = partial.TryGetValue(k, out var pv) ? pv : outp[k];
                outp[k] = ClampValue(k, v);
            }
            return outp;
        }
    }
}
