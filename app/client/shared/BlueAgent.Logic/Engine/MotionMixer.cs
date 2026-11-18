using System.Collections.Generic;

namespace BlueAgent.Logic.Engine
{
    /// <summary>
    /// 把六層表情疊起來 —— C# 版，對照 Kotlin `dev.deepsea.live2d.engine.mixer.MotionMixer`。
    ///
    /// 每一層的疊法**不一樣**（這是參考引擎的精髓，也是移植最容易做錯的地方）：
    /// <code>
    /// | 層                | 疊法                                                                    |
    /// | idle              | breath → 覆蓋；ADDITIVE ∪ {mouthSmile, browInnerUp} → 加；其餘 → 覆蓋   |
    /// | emotion           | breath → 取大；ADDITIVE ∪ {mouthSmile, mouthFrown, brow*} → 加；       |
    /// |                   | MAX 鍵 → 取大；其餘 → 覆蓋                                              |
    /// | reaction          | MAX 鍵 → 取大；ADDITIVE → 加；其餘 → 覆蓋                               |
    /// | speech            | mouthOpen → 取大；browOuterUp → 加；ADDITIVE → 加；**其餘不管**         |
    /// | speechPerformance | 跳過 mouthOpen／eyeOpen／eyeBlinkL／eyeBlinkR；MAX 鍵 → 取大；其餘 → 加  |
    /// | manual            | **直接覆蓋**                                                            |
    /// </code>
    /// 起點是 <see cref="Facs.CreateDefaultState"/>（eyeOpen=1、mouthSmile=0.04、breath=0.5…），不是全 0。
    /// 每一層套用完都夾限一次、整個混合完再夾限一次（照 Kotlin 的呼叫位置，別自己省）。
    /// </summary>
    public sealed class MotionMixer
    {
        public enum Mode
        {
            Idle,
            Emotion,
            Reaction,
            Speech,
            SpeechPerformance,
            Manual,
        }

        public Dictionary<string, float> Mix(
            IDictionary<string, float> idle = null,
            IDictionary<string, float> emotion = null,
            IDictionary<string, float> reaction = null,
            IDictionary<string, float> speech = null,
            IDictionary<string, float> speechPerformance = null,
            IDictionary<string, float> manual = null)
        {
            var result = Facs.CreateDefaultState();
            result = ApplyLayer(result, idle, Mode.Idle);
            result = ApplyLayer(result, emotion, Mode.Emotion);
            result = ApplyLayer(result, reaction, Mode.Reaction);
            result = ApplyLayer(result, speech, Mode.Speech);
            result = ApplyLayer(result, speechPerformance, Mode.SpeechPerformance);
            result = ApplyLayer(result, manual, Mode.Manual);
            return Facs.ClampState(result);
        }

        private static Dictionary<string, float> ApplyLayer(
            IDictionary<string, float> baseState, IDictionary<string, float> layer, Mode mode)
        {
            if (layer == null) return new Dictionary<string, float>(baseState);

            var result = new Dictionary<string, float>(baseState);
            foreach (var key in Facs.Keys)
            {
                if (!layer.TryGetValue(key, out var value)) continue;
                result.TryGetValue(key, out var cur);

                switch (mode)
                {
                    case Mode.Manual:
                        result[key] = value;
                        break;

                    case Mode.Speech:
                        if (key == "mouthOpen") result[key] = System.Math.Max(cur, value);
                        else if (key == "browOuterUp") result[key] = cur + value;
                        else if (Facs.Additive.Contains(key)) result[key] = cur + value;
                        // 其餘：參考引擎這層沒有 else ⇒ **不動**
                        break;

                    case Mode.SpeechPerformance:
                        if (key == "mouthOpen" || key == "eyeOpen" || key == "eyeBlinkL" || key == "eyeBlinkR") continue;
                        result[key] = Facs.Max.Contains(key) ? System.Math.Max(cur, value) : cur + value;
                        break;

                    case Mode.Idle:
                        if (key == "breath") result[key] = value;
                        else if (Facs.Additive.Contains(key) || key == "mouthSmile" || key == "browInnerUp")
                            result[key] = cur + value;
                        else result[key] = value;
                        break;

                    case Mode.Emotion:
                        if (key == "breath") result[key] = System.Math.Max(cur, value);
                        else if (Facs.Additive.Contains(key) || key == "mouthSmile" || key == "mouthFrown" ||
                                 key.StartsWith("brow"))
                            result[key] = cur + value;
                        else if (Facs.Max.Contains(key)) result[key] = System.Math.Max(cur, value);
                        else result[key] = value;
                        break;

                    case Mode.Reaction:
                        if (Facs.Max.Contains(key)) result[key] = System.Math.Max(cur, value);
                        else if (Facs.Additive.Contains(key)) result[key] = cur + value;
                        else result[key] = value;
                        break;
                }
            }
            return Facs.ClampState(result);
        }
    }
}
