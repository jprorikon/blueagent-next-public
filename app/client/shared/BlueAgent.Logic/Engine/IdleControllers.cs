using System;
using System.Collections.Generic;

namespace BlueAgent.Logic.Engine
{
    /// <summary>Float 區間（Kotlin `ClosedFloatingPointRange&lt;Float&gt;`；profile 的 `idleConfig` 用）。</summary>
    public readonly struct FloatRange
    {
        public readonly float Min;
        public readonly float Max;

        public FloatRange(float min, float max)
        {
            Min = min;
            Max = max;
        }
    }

    /// <summary>
    /// 待機層的五個控制器 ＋ 偏壓 —— C# 版，對照 Kotlin `idle/IdleControllers.kt`（參考引擎
    /// `engine/src/idle/` 的 1:1 移植）。
    ///
    /// 這一層是「沒有事件時角色自己在動」的全部來源（呼吸／微動／身體搖擺／視線／眨眼／偏壓）。
    /// 移植時**逐行照抄**：常數、順序、亂數消耗的次序都不能動（亂數是序列，順序一變，
    /// 後面每一幀都會不同 ⇒ fixture 會整片紅）。
    /// </summary>
    public sealed class BreathingController
    {
        private readonly RandomSource _random;
        private readonly double _phase;
        private readonly double _modulationPhase;
        private readonly double _secondaryPhase;

        public BreathingController(double seed = 17.0)
        {
            _random = EngineMath.SeededRandom(seed);
            _phase = _random.Next() * Math.PI * 2;
            _modulationPhase = _random.Next() * Math.PI * 2;
            _secondaryPhase = _random.Next() * Math.PI * 2;
        }

        public Dictionary<string, float> Update(float timeSeconds, float rate = 1f, float variance = 0.42f)
        {
            var r = EngineMath.Clamp(rate, 0.5f, 1.8f);
            var v = EngineMath.Clamp(variance, 0f, 1f);
            var t = (double)timeSeconds;
            var modulation = Math.Sin(t * 0.19 * r + _modulationPhase) * v;
            var cycle = t * 1.65 * r + _phase + modulation * 0.72;
            var secondary = Math.Sin(t * 0.73 * r + _secondaryPhase) * v;
            var breath = EngineMath.Clamp(0.5 + Math.Sin(cycle) * 0.31 + secondary * 0.045, 0.08, 0.92);
            var bodyY = Math.Sin(cycle - 0.3) * 0.022 + secondary * 0.004;
            return new Dictionary<string, float>
            {
                { "breath", (float)breath },
                { "bodyY", (float)bodyY },
            };
        }
    }

    /// <summary>`idle/MicroMotionController`：八顆相位／八顆頻率，兩條正弦疊出來的身體微動。</summary>
    public sealed class MicroMotionController
    {
        private readonly RandomSource _random;
        private readonly double[] _phases;
        private readonly double[] _rates;

        public MicroMotionController(double seed = 23.0)
        {
            _random = EngineMath.SeededRandom(seed);
            _phases = new double[8];
            for (var i = 0; i < 8; i++) _phases[i] = _random.Next() * Math.PI * 2;
            _rates = new double[8];
            for (var i = 0; i < 8; i++) _rates[i] = 0.86 + _random.Next() * 0.28;
        }

        public Dictionary<string, float> Update(float timeSeconds, float focusLevel, float gain = 1f)
        {
            var t = (double)timeSeconds;
            var damp = (1 - Math.Min(1f, focusLevel) * 0.65) * EngineMath.Clamp(gain, 0f, 2f);

            double Wave(int index, double frequency, double secondaryFrequency) =>
                Math.Sin(t * frequency * _rates[index] + _phases[index]) * 0.72 +
                Math.Sin(t * secondaryFrequency * _rates[index + 4] + _phases[index + 4]) * 0.28;

            return new Dictionary<string, float>
            {
                { "headX", (float)(Wave(0, 0.38, 0.17) * 0.02 * damp) },
                { "headY", (float)(Wave(1, 0.31, 0.13) * 0.016 * damp) },
                { "headZ", (float)(Wave(2, 0.24, 0.11) * 0.014 * damp) },
                { "mouthSmile", (float)(0.045 + Wave(3, 0.24, 0.09) * 0.018 * damp) },
                { "browInnerUp", (float)Math.Max(0.0, Wave(0, 0.18, 0.07) * 0.025 * damp) },
            };
        }
    }

    /// <summary>
    /// `idle/BodySwayController`：站姿搖擺 —— 挑一個目標姿勢 → 用五次緩動走過去 → 停住 → 再挑下一個。
    /// 範圍來自 profile 的 `idleConfig`（乘上各自的縮放），沒有設定才用預設範圍。
    /// </summary>
    public sealed class BodySwayController
    {
        private static readonly string[] Keys = { "bodyX", "bodyY", "bodyZ", "headX", "headY", "headZ" };

        private static readonly Dictionary<string, FloatRange> DefaultRanges = new Dictionary<string, FloatRange>
        {
            { "bodyX", new FloatRange(-0.045f, 0.045f) },
            { "bodyY", new FloatRange(-0.014f, 0.014f) },
            { "bodyZ", new FloatRange(-0.055f, 0.055f) },
            { "headX", new FloatRange(-0.028f, 0.028f) },
            { "headY", new FloatRange(-0.016f, 0.018f) },
            { "headZ", new FloatRange(-0.034f, 0.034f) },
        };

        private static readonly Dictionary<string, float> ProfileRangeScale = new Dictionary<string, float>
        {
            { "bodyX", 1f }, { "bodyY", 0.65f }, { "bodyZ", 1f },
            { "headX", 0.45f }, { "headY", 0.42f }, { "headZ", 0.58f },
        };

        private readonly RandomSource _random;

        private readonly float[] _from = new float[6];
        private readonly float[] _current = new float[6];
        private readonly float[] _target = new float[6];
        private float _moveStartedAt;
        private float _moveDuration = 2.2f;
        private float _holdUntil;

        public BodySwayController(double seed = 29.0)
        {
            _random = EngineMath.SeededRandom(seed);
        }

        private static int Idx(string key) => Array.IndexOf(Keys, key);

        public Dictionary<string, float> Update(
            float timeSeconds, float focusLevel, IReadOnlyDictionary<string, FloatRange> idleConfig, float gain = 1f)
        {
            var focus = EngineMath.Clamp(focusLevel, 0f, 1f);
            var motionGain = EngineMath.Clamp(gain, 0f, 4f);

            if (focus > 0.5f)
            {
                Recenter(0.06f + focus * 0.08f);
                return ToLayer((1f - focus * 0.76f) * motionGain);
            }

            if (timeSeconds >= _holdUntil) PickNextTarget(timeSeconds, idleConfig);

            var local = _moveDuration <= 0f
                ? 1f
                : EngineMath.Clamp((timeSeconds - _moveStartedAt) / _moveDuration, 0f, 1f);
            var eased = local * local * local * (local * (local * 6f - 15f) + 10f);

            for (var i = 0; i < Keys.Length; i++) _current[i] = EngineMath.Lerp(_from[i], _target[i], eased);
            return ToLayer(motionGain);
        }

        private void Recenter(float amount)
        {
            for (var i = 0; i < Keys.Length; i++)
            {
                _current[i] = EngineMath.Lerp(_current[i], 0f, amount);
                _from[i] = _current[i];
                _target[i] = 0f;
            }
        }

        private void PickNextTarget(float timeSeconds, IReadOnlyDictionary<string, FloatRange> idleConfig)
        {
            for (var i = 0; i < Keys.Length; i++) _from[i] = _current[i];

            var bodyX = PickValue("bodyX", idleConfig);
            var bodyZ = PickValue("bodyZ", idleConfig);
            var headXRange = RangeFor("headX", idleConfig);
            var headZRange = RangeFor("headZ", idleConfig);

            _target[Idx("bodyX")] = bodyX;
            _target[Idx("bodyY")] = PickValue("bodyY", idleConfig);
            _target[Idx("bodyZ")] = bodyZ;
            _target[Idx("headX")] = EngineMath.Clamp(PickValue("headX", idleConfig) + bodyX * 0.32f, headXRange.Min, headXRange.Max);
            _target[Idx("headY")] = PickValue("headY", idleConfig);
            _target[Idx("headZ")] = EngineMath.Clamp(PickValue("headZ", idleConfig) + bodyZ * 0.42f, headZRange.Min, headZRange.Max);

            _moveStartedAt = timeSeconds;
            _moveDuration = 1.45f + (float)_random.Next() * 2.35f;
            _holdUntil = timeSeconds + _moveDuration + 0.55f + (float)_random.Next() * 1.85f;
        }

        private float PickValue(string key, IReadOnlyDictionary<string, FloatRange> idleConfig)
        {
            var range = RangeFor(key, idleConfig);
            var centerBias = _random.Next() < 0.22 ? 0.38f : 1f;
            var center = (range.Min + range.Max) / 2f;
            var half = (range.Max - range.Min) / 2f * centerBias;
            return center - half + (float)_random.Next() * half * 2f;
        }

        private static FloatRange RangeFor(string key, IReadOnlyDictionary<string, FloatRange> idleConfig)
        {
            // 有設定的話就用設定的（乘上該鍵的縮放）；沒設定才退回預設範圍（與參考引擎同）
            if (idleConfig == null || !idleConfig.TryGetValue(key, out var configured))
                return DefaultRanges[key];
            var center = (configured.Min + configured.Max) / 2f;
            var half = (configured.Max - configured.Min) / 2f * ProfileRangeScale[key];
            return new FloatRange(center - half, center + half);
        }

        private Dictionary<string, float> ToLayer(float weight) => new Dictionary<string, float>
        {
            { "bodyX", _current[Idx("bodyX")] * weight },
            { "bodyY", _current[Idx("bodyY")] * weight },
            { "bodyZ", _current[Idx("bodyZ")] * weight },
            { "headX", _current[Idx("headX")] * weight },
            { "headY", _current[Idx("headY")] * weight },
            { "headZ", _current[Idx("headZ")] * weight },
        };
    }

    /// <summary>
    /// `idle/GazeController`：視線 —— 挑一個目標點 → 二段緩動移過去 → 停住 → 再挑下一個；
    /// 專注（focusLevel &gt; 0.5）時往中間收。
    /// </summary>
    public sealed class GazeController
    {
        private readonly RandomSource _random;
        private readonly float _stability;

        private float _fromX, _fromY;
        private float _currentX, _currentY;
        private float _targetX, _targetY;
        private float _moveStartedAt;
        private float _moveDuration = 0.8f;
        private float _holdUntil;

        public GazeController(double seed = 13.0, float stability = 0.72f)
        {
            _random = EngineMath.SeededRandom(seed);
            _stability = EngineMath.Clamp(stability, 0f, 1f);
        }

        public Dictionary<string, float> Update(
            float timeSeconds, float focusLevel, IReadOnlyDictionary<string, FloatRange> idleConfig)
        {
            if (focusLevel > 0.5f)
            {
                var t = Math.Min(1f, focusLevel);
                _currentX = EngineMath.Lerp(_currentX, 0f, 0.06f + t * 0.08f);
                _currentY = EngineMath.Lerp(_currentY, 0f, 0.06f + t * 0.08f);
                return new Dictionary<string, float> { { "gazeX", _currentX }, { "gazeY", _currentY } };
            }

            if (timeSeconds >= _holdUntil) PickNextTarget(timeSeconds, idleConfig);

            var local = _moveDuration <= 0f
                ? 1f
                : EngineMath.Clamp((timeSeconds - _moveStartedAt) / _moveDuration, 0f, 1f);
            // ⚠️ 這裡用的是 `pow(2)`（Kotlin 是 `.pow(2)`）——不要圖快寫成 `x*x`（最後一位可能不同）
            var eased = local < 0.5f
                ? 2f * local * local
                : 1f - (float)Math.Pow(-2f * local + 2f, 2) / 2f;
            _currentX = EngineMath.Lerp(_fromX, _targetX, eased);
            _currentY = EngineMath.Lerp(_fromY, _targetY, eased);

            return new Dictionary<string, float> { { "gazeX", _currentX }, { "gazeY", _currentY } };
        }

        private void PickNextTarget(float timeSeconds, IReadOnlyDictionary<string, FloatRange> idleConfig)
        {
            var xRange = Lookup(idleConfig, "gazeX", new FloatRange(-0.1f, 0.1f));
            var yRange = Lookup(idleConfig, "gazeY", new FloatRange(-0.05f, 0.07f));
            var rangeScale = 1.12f - _stability * 0.54f;

            _fromX = _currentX;
            _fromY = _currentY;
            _targetX = PickScaledTarget(xRange.Min, xRange.Max, rangeScale);
            _targetY = PickScaledTarget(yRange.Min, yRange.Max, rangeScale);
            _moveStartedAt = timeSeconds;
            _moveDuration = 0.55f + _stability * 0.5f + (float)_random.Next() * (0.9f + _stability * 0.62f);
            _holdUntil = timeSeconds + _moveDuration + 0.8f + _stability * 1.25f +
                (float)_random.Next() * (1.7f + _stability * 1.8f);
        }

        private static FloatRange Lookup(IReadOnlyDictionary<string, FloatRange> idleConfig, string key, FloatRange fallback) =>
            idleConfig != null && idleConfig.TryGetValue(key, out var r) ? r : fallback;

        private float PickScaledTarget(float min, float max, float scale)
        {
            var center = (min + max) / 2f;
            var half = (max - min) / 2f * scale;
            return center - half + (float)_random.Next() * half * 2f;
        }
    }

    /// <summary>
    /// `idle/BlinkController`：眨眼 —— 閉 0.065 → 停 0.035 → 開 0.13 秒；間隔 (3+rand*4)/rate；
    /// 專注時間隔拉長；偶爾補一個雙眨（專注時不補）。
    /// </summary>
    public sealed class BlinkController
    {
        private readonly RandomSource _random;
        private readonly float _rate;
        private float _nextBlinkAt;
        private float? _blinkStartedAt;
        private bool _doubleBlinkQueued;

        public BlinkController(double seed = 7.0, float rate = 1f)
        {
            _random = EngineMath.SeededRandom(seed);
            _rate = EngineMath.Clamp(rate, 0.25f, 2.5f);
            ScheduleNext(0f, 0f);
        }

        public void Defer(float timeSeconds, float duration)
        {
            _blinkStartedAt = null;
            _doubleBlinkQueued = false;
            var pauseScale = 1f / (float)Math.Sqrt(_rate);
            _nextBlinkAt = timeSeconds + Math.Max(0f, duration) + (0.75f + (float)_random.Next() * 1.15f) * pauseScale;
        }

        public Dictionary<string, float> Update(float timeSeconds, float focusLevel)
        {
            if (_blinkStartedAt == null && timeSeconds >= _nextBlinkAt) _blinkStartedAt = timeSeconds;

            var startedAt = _blinkStartedAt;
            if (startedAt == null)
            {
                return new Dictionary<string, float> { { "eyeBlinkL", 0f }, { "eyeBlinkR", 0f } };
            }

            var elapsed = timeSeconds - startedAt.Value;
            const float closeDuration = 0.065f;
            const float holdDuration = 0.035f;
            const float openDuration = 0.13f;
            var total = closeDuration + holdDuration + openDuration;
            var blink = 0f;

            if (elapsed <= closeDuration)
            {
                blink = EngineMath.Ease(EasingName.EaseIn, elapsed / closeDuration);
            }
            else if (elapsed <= closeDuration + holdDuration)
            {
                blink = 1f;
            }
            else if (elapsed <= total)
            {
                blink = 1f - EngineMath.Ease(EasingName.EaseOut, (elapsed - closeDuration - holdDuration) / openDuration);
            }
            else
            {
                var chance = focusLevel > 0.4f ? 0f : Math.Min(0.24f, 0.16f * _rate);
                // ⚠️ Kotlin 的 `&&` 會短路 ⇒ queued 為真時**不抽**那一顆亂數（照抄，否則序列就歪了）
                var shouldDouble = !_doubleBlinkQueued && _random.Next() < chance;
                _blinkStartedAt = null;
                if (shouldDouble)
                {
                    _doubleBlinkQueued = true;
                    _nextBlinkAt = timeSeconds + 0.18f + (float)_random.Next() * 0.12f;
                }
                else
                {
                    _doubleBlinkQueued = false;
                    ScheduleNext(timeSeconds, focusLevel);
                }
            }

            return new Dictionary<string, float> { { "eyeBlinkL", blink }, { "eyeBlinkR", blink } };
        }

        private void ScheduleNext(float timeSeconds, float focusLevel)
        {
            var baseValue = (3f + (float)_random.Next() * 4f) / _rate;
            var focusedExtra = focusLevel * 1.1f;
            _nextBlinkAt = timeSeconds + baseValue + focusedExtra;
        }
    }

    /// <summary>`idle/IdleBiasController`：表情殘留（暫時把中立姿勢往某個表情拉一段時間）。</summary>
    public sealed class IdleBiasController
    {
        private IReadOnlyDictionary<string, float> _bias = new Dictionary<string, float>();
        private float _startedAt;
        private float _duration;

        public void SetBias(IReadOnlyDictionary<string, float> bias, float duration, float timeSeconds)
        {
            _bias = new Dictionary<string, float>(bias);
            _duration = Math.Max(0.1f, duration);
            _startedAt = timeSeconds;
        }

        public void Reset()
        {
            _bias = new Dictionary<string, float>();
            _duration = 0f;
            _startedAt = 0f;
        }

        public Dictionary<string, float> Update(float timeSeconds)
        {
            if (_duration <= 0f) return new Dictionary<string, float>();
            var progress = Math.Min(1f, (timeSeconds - _startedAt) / _duration);
            if (progress >= 1f)
            {
                Reset();
                return new Dictionary<string, float>();
            }
            var residue = (float)Math.Pow(1f - progress, 0.82);
            return Facs.Scale(_bias, residue);
        }
    }
}
