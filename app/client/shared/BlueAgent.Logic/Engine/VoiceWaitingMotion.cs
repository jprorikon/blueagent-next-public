using System;
using System.Collections.Generic;

namespace BlueAgent.Logic.Engine
{
    /// <summary>`VoiceWaitingMotionOptions`。三個都是選配（沒給就從 VAD 推）。</summary>
    public sealed class VoiceWaitingMotionOptions
    {
        public readonly string Emotion;
        public readonly double? Intensity;
        public readonly VadVector? Vad;

        public VoiceWaitingMotionOptions(string emotion = null, double? intensity = null, VadVector? vad = null)
        {
            Emotion = emotion;
            Intensity = intensity;
            Vad = vad;
        }
    }

    /// <summary>`VoiceWaitingMotionStartInfo`：這次挑到哪個模板、多長、多大、多快。</summary>
    public sealed class VoiceWaitingMotionStartInfo
    {
        public readonly string Label;
        public readonly double Duration;
        public readonly double Amplitude;
        public readonly double Tempo;
        public readonly string Emotion;
        public readonly VadVector Vad;

        public VoiceWaitingMotionStartInfo(
            string label, double duration, double amplitude, double tempo, string emotion, VadVector vad)
        {
            Label = label;
            Duration = duration;
            Amplitude = amplitude;
            Tempo = tempo;
            Emotion = emotion;
            Vad = vad;
        }
    }

    /// <summary>
    /// `speech/VoiceWaitingMotionController.kt` 的 1:1 移植：**等待動作**
    /// （「她正在等你說完／等你回應」時身體不該是死的，該有緩慢的搖擺／歪頭／點頭）。
    ///
    /// 一次 `Start()` 會挑一個模板、把整段動作**預先算成一串關鍵影格**（`RuntimeExpressionKeyframe`），
    /// 之後每幀只做「時間軸求值 ＋ 六顆身體／頭部通道乘上 `bodyMotionGain`」。
    ///
    /// 七個模板（權重由 VAD／情緒決定，**不是均勻抽**）：
    /// `slow-sway`／`figure-eight`／`curious-lean`／`shy-rock`／`buoyant-bob`／`soft-settle`／`contained-tension`。
    ///
    /// ⚠️ **亂數消耗次序**（照抄，錯了整組換模板）：`side` → `duration` → `amplitude` → `tempo`
    /// → `PickTemplate`（1 抽）→ 各模板自己的額外抽（`figure-eight` 的 `gazeLead`、
    /// `curious-lean` 的 `tilt`、`buoyant-bob` 的 `bob`、`contained-tension` 的 `tension`）。
    /// 前四個都在挑模板**之前**，所以「模板」與「幅度」是同一條序列上的先後關係。
    ///
    /// ⚠️ 另一個容易漏的：`Update()` 回傳的物件**一定包含**六顆身體／頭部鍵（缺席時補 0），
    /// 因為它是「原圖 ＋ 覆寫六顆再夾限」。缺席與 0 在混合時意思不同，不能省。
    /// </summary>
    public sealed class VoiceWaitingMotionController
    {
        private const double BaseTemplateDurationSeconds = 6.0;
        private const double WaitingDurationSeconds = 9.0;
        private const double WaitingAmplitudeScale = 0.4;

        private sealed class Motion
        {
            public readonly string Label;
            public readonly double StartedAt;
            public readonly double Duration;
            public readonly IReadOnlyList<RuntimeExpressionKeyframe> Frames;

            public Motion(string label, double startedAt, double duration, IReadOnlyList<RuntimeExpressionKeyframe> frames)
            {
                Label = label;
                StartedAt = startedAt;
                Duration = duration;
                Frames = frames;
            }
        }

        private sealed class Style
        {
            public readonly string Emotion;
            public readonly double Valence;
            public readonly double Arousal;
            public readonly double Dominance;
            public readonly double Intensity;
            public readonly Dictionary<string, float> PostureBias;

            public Style(
                string emotion, double valence, double arousal, double dominance, double intensity,
                Dictionary<string, float> postureBias)
            {
                Emotion = emotion;
                Valence = valence;
                Arousal = arousal;
                Dominance = dominance;
                Intensity = intensity;
                PostureBias = postureBias;
            }
        }

        private sealed class Context
        {
            public readonly double Side;
            public readonly double Amplitude;
            public readonly double Tempo;
            public readonly RandomSource Random;
            public readonly Style Style;

            public Context(double side, double amplitude, double tempo, RandomSource random, Style style)
            {
                Side = side;
                Amplitude = amplitude;
                Tempo = tempo;
                Random = random;
                Style = style;
            }
        }

        private sealed class Template
        {
            public readonly string Label;
            public readonly Func<Style, double> Weight;
            public readonly Func<Context, double, IReadOnlyList<RuntimeExpressionKeyframe>> Build;

            public Template(
                string label,
                Func<Style, double> weight,
                Func<Context, double, IReadOnlyList<RuntimeExpressionKeyframe>> build)
            {
                Label = label;
                Weight = weight;
                Build = build;
            }
        }

        private Motion _motion;

        public VoiceWaitingMotionStartInfo Start(
            double timeSeconds,
            double? seed = null,
            VoiceWaitingMotionOptions options = null)
        {
            var resolvedSeed = seed ?? (double)EngineMath.RoundJavaToLong(timeSeconds * 1000.0);
            var opts = options ?? new VoiceWaitingMotionOptions();
            var random = EngineMath.SeededRandom(resolvedSeed);
            var side = random.Next() < 0.5 ? -1.0 : 1.0;
            var style = CreateStyle(opts, side);
            var duration = EngineMath.Clamp(
                WaitingDurationSeconds + (random.Next() - 0.5) * 0.4 - style.Arousal * 0.28, 8.6, 9.4);
            var amplitude = (0.82 + random.Next() * 0.18) * WaitingAmplitudeScale * WaitingAmplitudeFactor(style);
            var tempo = EngineMath.Clamp((0.92 + random.Next() * 0.16) * (1 - style.Arousal * 0.13), 0.78, 1.24);
            var template = PickTemplate(random, style);

            _motion = new Motion(
                label: template.Label,
                startedAt: timeSeconds,
                duration: duration,
                frames: template.Build(new Context(side, amplitude, tempo, random, style), duration));

            return new VoiceWaitingMotionStartInfo(
                label: template.Label,
                duration: duration,
                amplitude: amplitude,
                tempo: tempo,
                emotion: style.Emotion,
                vad: new VadVector(style.Valence, style.Arousal, style.Dominance));
        }

        public void Reset() => _motion = null;

        public Dictionary<string, float> Update(double timeSeconds, double bodyMotionGain = 1.0)
        {
            var current = _motion;
            if (current == null) return new Dictionary<string, float>();

            var elapsed = timeSeconds - current.StartedAt;
            if (elapsed >= current.Duration)
            {
                _motion = null;
                return new Dictionary<string, float>();
            }

            var layer = ExpressionTimeline.Evaluate(current.Frames, (float)elapsed);
            var gain = Math.Max(0.0, Math.Min(bodyMotionGain, 4.0));
            var gainF = (float)gain;

            var outp = new Dictionary<string, float>(layer);
            outp["headX"] = (layer.TryGetValue("headX", out var headX) ? headX : 0f) * gainF;
            outp["headY"] = (layer.TryGetValue("headY", out var headY) ? headY : 0f) * gainF;
            outp["headZ"] = (layer.TryGetValue("headZ", out var headZ) ? headZ : 0f) * gainF;
            outp["bodyX"] = (layer.TryGetValue("bodyX", out var bodyX) ? bodyX : 0f) * gainF;
            outp["bodyY"] = (layer.TryGetValue("bodyY", out var bodyY) ? bodyY : 0f) * gainF;
            outp["bodyZ"] = (layer.TryGetValue("bodyZ", out var bodyZ) ? bodyZ : 0f) * gainF;
            return Facs.ClampState(outp);
        }

        // ── 模板 ────────────────────────────────────────────────────────────

        private static readonly Template[] Templates =
        {
            new Template("slow-sway", s => 1.15 + Math.Max(-s.Arousal, 0.0) * 0.55 + Math.Max(s.Valence, 0.0) * 0.28, BuildSlowSway),
            new Template("figure-eight", s => 0.75 + Math.Max(s.Arousal, 0.0) * 0.38 + Math.Max(s.Dominance, 0.0) * 0.24, BuildFigureEight),
            new Template("curious-lean", s => 0.52 + EmotionWeight(s, new[] { "curious", "confused", "surprised" }, 2.25) + Math.Max(s.Arousal, 0.0) * 0.16, BuildCuriousLean),
            new Template("shy-rock", s => 0.54 + EmotionWeight(s, new[] { "shy", "affectionate" }, 2.2) + Math.Max(-s.Dominance, 0.0) * 0.72, BuildShyRock),
            new Template("buoyant-bob", s => 0.52 + EmotionWeight(s, new[] { "happy", "excited" }, 2.2) + Math.Max(s.Valence, 0.0) * 0.74, BuildBuoyantBob),
            new Template("soft-settle", s => 0.44 + EmotionWeight(s, new[] { "sad", "tired", "concerned", "calm" }, 1.9) +
                Math.Max(-s.Valence, 0.0) * 0.82 + Math.Max(-s.Arousal, 0.0) * 0.55, BuildSoftSettle),
            new Template("contained-tension", s => 0.34 + EmotionWeight(s, new[] { "anxiety", "anger", "angry" }, 2.15) +
                Math.Max(s.Arousal, 0.0) * 0.58 + Math.Max(-s.Valence, 0.0) * 0.45, BuildContainedTension),
        };

        private static Template PickTemplate(RandomSource random, Style style)
        {
            var weights = new double[Templates.Length];
            var total = 0.0;
            for (var i = 0; i < Templates.Length; i++)
            {
                weights[i] = Math.Max(0.05, Templates[i].Weight(style));
                total += weights[i];
            }
            var roll = random.Next() * total;
            for (var i = 0; i < Templates.Length; i++)
            {
                roll -= weights[i];
                if (roll <= 0) return Templates[i];
            }
            return Templates[0];
        }

        // ── style ───────────────────────────────────────────────────────────

        private static Style CreateStyle(VoiceWaitingMotionOptions options, double side)
        {
            var vad = NormalizeVad(options.Vad);
            var emotion = NormalizeEmotion(options.Emotion);
            var intensity = EngineMath.Clamp(options.Intensity ?? EstimateVadIntensity(vad), 0.0, 1.0);
            var positive = Math.Max(vad.Valence, 0.0);
            var negative = Math.Max(-vad.Valence, 0.0);
            var highArousal = Math.Max(vad.Arousal, 0.0);
            var lowArousal = Math.Max(-vad.Arousal, 0.0);
            var confident = Math.Max(vad.Dominance, 0.0);
            var withdrawn = Math.Max(-vad.Dominance, 0.0);
            var shy = IsEmotion(emotion, new[] { "shy", "affectionate" });
            var anxious = IsEmotion(emotion, new[] { "anxiety" }) || (negative > 0.32 && highArousal > 0.34);
            var angry = IsEmotion(emotion, new[] { "anger", "angry" }) ||
                (negative > 0.38 && highArousal > 0.38 && confident > 0.12);
            var curious = IsEmotion(emotion, new[] { "curious", "confused", "surprised" });

            var postureBias = Facs.ClampState(new Dictionary<string, float>
            {
                ["headY"] = (float)(positive * 0.024 - negative * 0.036 + highArousal * 0.012 - lowArousal * 0.008),
                ["headZ"] = (float)(side * (withdrawn * 0.036 + (shy ? 0.032 : 0.0) - confident * 0.014)),
                ["bodyY"] = (float)(positive * 0.026 - negative * 0.03 - lowArousal * 0.008),
                ["bodyZ"] = (float)(side * (withdrawn * 0.026 + (shy ? 0.016 : 0.0) - confident * 0.012)),
                ["gazeX"] = (float)(side * (withdrawn * 0.036 + (shy ? 0.026 : 0.0) - confident * 0.01)),
                ["gazeY"] = (float)(positive * 0.016 - negative * 0.05 + highArousal * 0.012),
                ["eyeSmile"] = (float)(positive * 0.06 + (shy ? 0.04 : 0.0)),
                ["eyeSquint"] = (float)(angry ? 0.036 + intensity * 0.018 : 0.0),
                ["browOuterUp"] = (float)(highArousal * 0.034 + (curious ? 0.046 : 0.0)),
                ["browInnerUp"] = (float)(negative * 0.052 + withdrawn * 0.034 + (anxious ? 0.048 : 0.0)),
                ["browDown"] = (float)(angry ? 0.048 + intensity * 0.028 : 0.0),
                ["mouthSmile"] = (float)(positive * 0.06 + (shy ? 0.034 : 0.0)),
                ["mouthFrown"] = (float)(negative * 0.044),
                ["blush"] = (float)(shy ? 0.066 + positive * 0.028 : 0.0),
                ["sweat"] = (float)(anxious ? 0.046 + highArousal * 0.026 : 0.0),
            });

            return new Style(
                emotion: emotion,
                valence: vad.Valence,
                arousal: vad.Arousal,
                dominance: vad.Dominance,
                intensity: intensity,
                postureBias: postureBias);
        }

        private static VadVector NormalizeVad(VadVector? vad) => new VadVector(
            EngineMath.Clamp(vad?.Valence ?? 0.0, -1.0, 1.0),
            EngineMath.Clamp(vad?.Arousal ?? 0.0, -1.0, 1.0),
            EngineMath.Clamp(vad?.Dominance ?? 0.0, -1.0, 1.0));

        private static double EstimateVadIntensity(VadVector vad) =>
            EngineMath.Clamp((Math.Abs(vad.Valence) + Math.Abs(vad.Arousal) * 0.9 + Math.Abs(vad.Dominance) * 0.7) / 2.6, 0.0, 1.0);

        private static double WaitingAmplitudeFactor(Style style)
        {
            var highArousal = Math.Max(style.Arousal, 0.0);
            var lowArousal = Math.Max(-style.Arousal, 0.0);
            var negative = Math.Max(-style.Valence, 0.0);
            var confident = Math.Max(style.Dominance, 0.0);
            var withdrawn = Math.Max(-style.Dominance, 0.0);

            return EngineMath.Clamp(
                1 + highArousal * 0.14 - lowArousal * 0.1 + confident * 0.05 -
                    negative * 0.08 - withdrawn * 0.04 + style.Intensity * 0.08,
                0.72, 1.18);
        }

        private static double EmotionWeight(Style style, IReadOnlyList<string> emotions, double boost) =>
            IsEmotion(style.Emotion, emotions) ? boost : 0.0;

        private static bool IsEmotion(string emotion, IReadOnlyList<string> emotions)
        {
            for (var i = 0; i < emotions.Count; i++)
            {
                if (emotions[i] == emotion) return true;
            }
            return false;
        }

        /// <summary>⚠️ 空字串回 `neutral`；`soft-*` 這幾個是「橋側的柔和情緒名」。</summary>
        private static string NormalizeEmotion(string emotion)
        {
            var n = emotion == null ? "" : KotlinText.Trim(emotion).ToLowerInvariant();
            switch (n)
            {
                case "soft-happy":
                case "soft-positive":
                    return "happy";
                case "soft-calm":
                    return "calm";
                case "soft-curious":
                    return "curious";
                case "soft-shy":
                    return "shy";
                case "soft-uneasy":
                    return "anxiety";
                case "soft-low":
                    return "sad";
                case "soft-steady":
                    return "neutral";
                default:
                    return n.Length > 0 ? n : "neutral";
            }
        }

        // ── 時間軸組裝的小工具（照抄參考實作的三支） ──────────────────────────

        /// <summary>`createWaitingTimeScaler`：模板以「6 秒」為基準寫死，實際長度按比例伸縮。</summary>
        private static Func<double, double> TimeScaler(double duration)
        {
            var scale = duration / BaseTemplateDurationSeconds;
            return time => time * scale;
        }

        /// <summary>`styledFrame`：模板給的影格再**疊上姿勢偏置**（權重預設 1）。</summary>
        private static RuntimeExpressionKeyframe StyledFrame(
            Context context, double time, double duration, EasingName easing,
            Dictionary<string, float> facs, double styleWeight = 1.0) =>
            Frame(time, duration, easing, Facs.Add(facs, context.Style.PostureBias, (float)styleWeight));

        /// <summary>`frame`：⚠️ `duration` 至少 0.2 秒（0 會讓求值除以零）。</summary>
        private static RuntimeExpressionKeyframe Frame(double time, double duration, EasingName easing, IReadOnlyDictionary<string, float> facs) =>
            new RuntimeExpressionKeyframe((float)time, (float)Math.Max(0.2, duration), easing, facs);

        private static Dictionary<string, float> SwayFrame(double side, double amplitude, int phase)
        {
            var headZBase = phase == 1 ? 0.52 : phase == 2 ? 0.62 : 0.42;
            var bodyZBase = phase == 1 ? 0.42 : phase == 2 ? 0.5 : 0.34;

            return Facs.ClampState(new Dictionary<string, float>
            {
                ["headX"] = (float)(side * amplitude * 0.18),
                ["headY"] = (float)(amplitude * 0.1),
                ["headZ"] = (float)(side * amplitude * headZBase),
                ["bodyX"] = (float)(side * amplitude * 0.22),
                ["bodyY"] = (float)(amplitude * 0.16),
                ["bodyZ"] = (float)(side * amplitude * bodyZBase),
                ["gazeX"] = (float)(-side * amplitude * 0.2),
                ["gazeY"] = (float)(amplitude * 0.08),
                ["eyeSmile"] = (float)(amplitude * 0.16),
                ["browOuterUp"] = (float)(amplitude * 0.1),
                ["mouthSmile"] = (float)(amplitude * 0.16),
            });
        }

        /// <summary>收尾影格：把所有相關通道歸零（不是空物件 —— 缺席與 0 在混合時意思不同）。</summary>
        private static Dictionary<string, float> SwayRestFrame() => new Dictionary<string, float>
        {
            ["browInnerUp"] = 0f,
            ["headX"] = 0f,
            ["headY"] = 0f,
            ["headZ"] = 0f,
            ["bodyX"] = 0f,
            ["bodyY"] = 0f,
            ["bodyZ"] = 0f,
            ["gazeX"] = 0f,
            ["gazeY"] = 0f,
            ["eyeSmile"] = 0f,
            ["eyeSquint"] = 0f,
            ["browOuterUp"] = 0f,
            ["mouthSmile"] = 0f,
            ["blush"] = 0f,
        };

        // ── 七個模板的影格表（數值照抄） ─────────────────────────────────────

        private static IReadOnlyList<RuntimeExpressionKeyframe> BuildSlowSway(Context context, double duration)
        {
            var side = context.Side;
            var amplitude = context.Amplitude;
            var tempo = context.Tempo;
            var t = TimeScaler(duration);

            return new[]
            {
                StyledFrame(context, 0.0, t(1.18 * tempo), EasingName.EaseOut, SwayFrame(side, amplitude, 1)),
                StyledFrame(context, t(1.44 * tempo), t(1.38 * tempo), EasingName.EaseInOut, SwayFrame(-side, amplitude * 0.92, 2)),
                StyledFrame(context, t(3 * tempo), t(1.22 * tempo), EasingName.EaseInOut, SwayFrame(side, amplitude * 0.62, 3)),
                Frame(t(4.5), duration - t(4.5), EasingName.EaseOut, SwayRestFrame()),
            };
        }

        private static IReadOnlyList<RuntimeExpressionKeyframe> BuildFigureEight(Context context, double duration)
        {
            var side = context.Side;
            var amplitude = context.Amplitude;
            var tempo = context.Tempo;
            var t = TimeScaler(duration);
            var gazeLead = 0.14 + context.Random.Next() * 0.08;

            return new[]
            {
                StyledFrame(context, 0.0, t(0.96 * tempo), EasingName.EaseOut, Facs.ClampState(new Dictionary<string, float>
                {
                    ["headX"] = (float)(side * amplitude * 0.2),
                    ["headY"] = (float)(amplitude * 0.1),
                    ["headZ"] = (float)(side * amplitude * 0.42),
                    ["bodyX"] = (float)(-side * amplitude * 0.16),
                    ["bodyY"] = (float)(amplitude * 0.12),
                    ["bodyZ"] = (float)(side * amplitude * 0.32),
                    ["gazeX"] = (float)(-side * amplitude * gazeLead),
                    ["gazeY"] = (float)(amplitude * 0.08),
                    ["eyeSmile"] = (float)(amplitude * 0.12),
                    ["mouthSmile"] = (float)(amplitude * 0.12),
                })),
                StyledFrame(context, t(1.2 * tempo), t(1.08 * tempo), EasingName.EaseInOut, Facs.ClampState(new Dictionary<string, float>
                {
                    ["headX"] = (float)(-side * amplitude * 0.18),
                    ["headY"] = (float)(-amplitude * 0.05),
                    ["headZ"] = (float)(-side * amplitude * 0.36),
                    ["bodyX"] = (float)(side * amplitude * 0.15),
                    ["bodyY"] = (float)(amplitude * 0.08),
                    ["bodyZ"] = (float)(-side * amplitude * 0.3),
                    ["gazeX"] = (float)(side * amplitude * gazeLead),
                    ["gazeY"] = (float)(-amplitude * 0.04),
                    ["browOuterUp"] = (float)(amplitude * 0.08),
                    ["mouthSmile"] = (float)(amplitude * 0.1),
                })),
                StyledFrame(context, t(2.55 * tempo), t(1.06 * tempo), EasingName.EaseInOut, Facs.ClampState(new Dictionary<string, float>
                {
                    ["headX"] = (float)(side * amplitude * 0.15),
                    ["headY"] = (float)(amplitude * 0.06),
                    ["headZ"] = (float)(side * amplitude * 0.3),
                    ["bodyX"] = (float)(side * amplitude * 0.1),
                    ["bodyY"] = (float)(amplitude * 0.1),
                    ["bodyZ"] = (float)(side * amplitude * 0.24),
                    ["gazeX"] = (float)(-side * amplitude * gazeLead * 0.7),
                    ["gazeY"] = (float)(amplitude * 0.05),
                    ["eyeSmile"] = (float)(amplitude * 0.1),
                })),
                Frame(t(4.3), duration - t(4.3), EasingName.EaseOut, SwayRestFrame()),
            };
        }

        private static IReadOnlyList<RuntimeExpressionKeyframe> BuildCuriousLean(Context context, double duration)
        {
            var side = context.Side;
            var amplitude = context.Amplitude;
            var tempo = context.Tempo;
            var t = TimeScaler(duration);
            var tilt = 0.34 + context.Random.Next() * 0.12;

            return new[]
            {
                StyledFrame(context, 0.0, t(1.08 * tempo), EasingName.EaseOut, Facs.ClampState(new Dictionary<string, float>
                {
                    ["headX"] = (float)(side * amplitude * 0.12),
                    ["headY"] = (float)(amplitude * 0.18),
                    ["headZ"] = (float)(side * amplitude * tilt),
                    ["bodyY"] = (float)(amplitude * 0.3),
                    ["bodyZ"] = (float)(side * amplitude * 0.18),
                    ["gazeX"] = (float)(side * amplitude * 0.18),
                    ["gazeY"] = (float)(amplitude * 0.14),
                    ["browOuterUp"] = (float)(amplitude * 0.16),
                    ["eyeSmile"] = (float)(amplitude * 0.08),
                    ["mouthSmile"] = (float)(amplitude * 0.08),
                })),
                StyledFrame(context, t(1.65 * tempo), t(1.18 * tempo), EasingName.EaseInOut, Facs.ClampState(new Dictionary<string, float>
                {
                    ["headX"] = (float)(-side * amplitude * 0.08),
                    ["headY"] = (float)(amplitude * 0.12),
                    ["headZ"] = (float)(-side * amplitude * 0.22),
                    ["bodyY"] = (float)(amplitude * 0.2),
                    ["bodyZ"] = (float)(-side * amplitude * 0.12),
                    ["gazeX"] = (float)(-side * amplitude * 0.22),
                    ["gazeY"] = (float)(amplitude * 0.08),
                    ["browOuterUp"] = (float)(amplitude * 0.12),
                    ["eyeSquint"] = (float)(amplitude * 0.06),
                })),
                StyledFrame(context, t(3.35), t(1.05), EasingName.EaseInOut, Facs.ClampState(new Dictionary<string, float>
                {
                    ["headY"] = (float)(amplitude * 0.06),
                    ["bodyY"] = (float)(amplitude * 0.1),
                    ["gazeY"] = (float)(amplitude * 0.04),
                    ["eyeSmile"] = (float)(amplitude * 0.08),
                    ["mouthSmile"] = (float)(amplitude * 0.08),
                })),
                Frame(t(4.55), duration - t(4.55), EasingName.EaseOut, SwayRestFrame()),
            };
        }

        private static IReadOnlyList<RuntimeExpressionKeyframe> BuildShyRock(Context context, double duration)
        {
            var side = context.Side;
            var amplitude = context.Amplitude;
            var tempo = context.Tempo;
            var t = TimeScaler(duration);

            return new[]
            {
                StyledFrame(context, 0.0, t(1.15 * tempo), EasingName.EaseOut, Facs.ClampState(new Dictionary<string, float>
                {
                    ["headY"] = (float)(-amplitude * 0.2),
                    ["headZ"] = (float)(-side * amplitude * 0.34),
                    ["bodyX"] = (float)(side * amplitude * 0.12),
                    ["bodyY"] = (float)(-amplitude * 0.12),
                    ["bodyZ"] = (float)(-side * amplitude * 0.28),
                    ["gazeX"] = (float)(side * amplitude * 0.2),
                    ["gazeY"] = (float)(-amplitude * 0.14),
                    ["browInnerUp"] = (float)(amplitude * 0.12),
                    ["blush"] = (float)(amplitude * 0.12),
                    ["mouthSmile"] = (float)(amplitude * 0.1),
                })),
                StyledFrame(context, t(1.68 * tempo), t(1.25 * tempo), EasingName.EaseInOut, Facs.ClampState(new Dictionary<string, float>
                {
                    ["headY"] = (float)(-amplitude * 0.16),
                    ["headZ"] = (float)(side * amplitude * 0.28),
                    ["bodyX"] = (float)(-side * amplitude * 0.1),
                    ["bodyY"] = (float)(-amplitude * 0.08),
                    ["bodyZ"] = (float)(side * amplitude * 0.22),
                    ["gazeX"] = (float)(-side * amplitude * 0.14),
                    ["gazeY"] = (float)(-amplitude * 0.1),
                    ["eyeSmile"] = (float)(amplitude * 0.1),
                    ["blush"] = (float)(amplitude * 0.14),
                    ["mouthSmile"] = (float)(amplitude * 0.12),
                })),
                StyledFrame(context, t(3.3), t(1.05), EasingName.EaseInOut, Facs.ClampState(new Dictionary<string, float>
                {
                    ["headY"] = (float)(-amplitude * 0.08),
                    ["gazeY"] = (float)(-amplitude * 0.05),
                    ["blush"] = (float)(amplitude * 0.08),
                    ["mouthSmile"] = (float)(amplitude * 0.08),
                })),
                Frame(t(4.55), duration - t(4.55), EasingName.EaseOut, SwayRestFrame()),
            };
        }

        private static IReadOnlyList<RuntimeExpressionKeyframe> BuildBuoyantBob(Context context, double duration)
        {
            var side = context.Side;
            var amplitude = context.Amplitude;
            var tempo = context.Tempo;
            var t = TimeScaler(duration);
            var bob = 0.2 + context.Random.Next() * 0.08;

            return new[]
            {
                StyledFrame(context, 0.0, t(0.9 * tempo), EasingName.EaseOut, Facs.ClampState(new Dictionary<string, float>
                {
                    ["headY"] = (float)(amplitude * bob),
                    ["headZ"] = (float)(side * amplitude * 0.2),
                    ["bodyY"] = (float)(amplitude * 0.28),
                    ["bodyZ"] = (float)(side * amplitude * 0.16),
                    ["gazeY"] = (float)(amplitude * 0.08),
                    ["eyeSmile"] = (float)(amplitude * 0.16),
                    ["browOuterUp"] = (float)(amplitude * 0.08),
                    ["mouthSmile"] = (float)(amplitude * 0.16),
                })),
                StyledFrame(context, t(1.18 * tempo), t(0.95 * tempo), EasingName.EaseInOut, Facs.ClampState(new Dictionary<string, float>
                {
                    ["headY"] = (float)(-amplitude * 0.08),
                    ["headZ"] = (float)(-side * amplitude * 0.18),
                    ["bodyY"] = (float)(-amplitude * 0.08),
                    ["bodyZ"] = (float)(-side * amplitude * 0.12),
                    ["gazeY"] = (float)(-amplitude * 0.04),
                    ["eyeSmile"] = (float)(amplitude * 0.12),
                    ["mouthSmile"] = (float)(amplitude * 0.12),
                })),
                StyledFrame(context, t(2.55 * tempo), t(1.12 * tempo), EasingName.EaseInOut, Facs.ClampState(new Dictionary<string, float>
                {
                    ["headY"] = (float)(amplitude * bob * 0.7),
                    ["headZ"] = (float)(side * amplitude * 0.16),
                    ["bodyY"] = (float)(amplitude * 0.18),
                    ["bodyZ"] = (float)(side * amplitude * 0.1),
                    ["gazeX"] = (float)(-side * amplitude * 0.1),
                    ["gazeY"] = (float)(amplitude * 0.05),
                    ["eyeSmile"] = (float)(amplitude * 0.14),
                    ["mouthSmile"] = (float)(amplitude * 0.14),
                })),
                Frame(t(4.42), duration - t(4.42), EasingName.EaseOut, SwayRestFrame()),
            };
        }

        private static IReadOnlyList<RuntimeExpressionKeyframe> BuildSoftSettle(Context context, double duration)
        {
            var side = context.Side;
            var amplitude = context.Amplitude;
            var tempo = context.Tempo;
            var t = TimeScaler(duration);

            return new[]
            {
                StyledFrame(context, 0.0, t(1.34 * tempo), EasingName.EaseOut, Facs.ClampState(new Dictionary<string, float>
                {
                    ["headY"] = (float)(-amplitude * 0.14),
                    ["headZ"] = (float)(side * amplitude * 0.18),
                    ["bodyY"] = (float)(-amplitude * 0.1),
                    ["bodyZ"] = (float)(side * amplitude * 0.12),
                    ["gazeX"] = (float)(side * amplitude * 0.08),
                    ["gazeY"] = (float)(-amplitude * 0.12),
                    ["browInnerUp"] = (float)(amplitude * 0.1),
                    ["mouthFrown"] = (float)(amplitude * 0.06),
                })),
                StyledFrame(context, t(1.82 * tempo), t(1.42 * tempo), EasingName.EaseInOut, Facs.ClampState(new Dictionary<string, float>
                {
                    ["headY"] = (float)(-amplitude * 0.07),
                    ["headZ"] = (float)(-side * amplitude * 0.12),
                    ["bodyY"] = (float)(-amplitude * 0.06),
                    ["bodyZ"] = (float)(-side * amplitude * 0.08),
                    ["gazeX"] = (float)(-side * amplitude * 0.06),
                    ["gazeY"] = (float)(-amplitude * 0.08),
                    ["browInnerUp"] = (float)(amplitude * 0.08),
                    ["eyeSmile"] = (float)(amplitude * 0.04),
                })),
                StyledFrame(context, t(3.52), t(1.08), EasingName.EaseInOut, Facs.ClampState(new Dictionary<string, float>
                {
                    ["headY"] = (float)(-amplitude * 0.04),
                    ["bodyY"] = (float)(-amplitude * 0.04),
                    ["gazeY"] = (float)(-amplitude * 0.05),
                    ["browInnerUp"] = (float)(amplitude * 0.06),
                })),
                Frame(t(4.72), duration - t(4.72), EasingName.EaseOut, SwayRestFrame()),
            };
        }

        private static IReadOnlyList<RuntimeExpressionKeyframe> BuildContainedTension(Context context, double duration)
        {
            var side = context.Side;
            var amplitude = context.Amplitude;
            var tempo = context.Tempo;
            var t = TimeScaler(duration);
            var tension = 0.76 + context.Random.Next() * 0.18;

            return new[]
            {
                StyledFrame(context, 0.0, t(0.98 * tempo), EasingName.EaseOut, Facs.ClampState(new Dictionary<string, float>
                {
                    ["headX"] = (float)(side * amplitude * 0.08),
                    ["headY"] = (float)(-amplitude * 0.04),
                    ["headZ"] = (float)(side * amplitude * 0.24 * tension),
                    ["bodyX"] = (float)(side * amplitude * 0.1),
                    ["bodyY"] = (float)(-amplitude * 0.04),
                    ["bodyZ"] = (float)(-side * amplitude * 0.16 * tension),
                    ["gazeX"] = (float)(-side * amplitude * 0.12),
                    ["gazeY"] = (float)(-amplitude * 0.03),
                    ["browDown"] = (float)(amplitude * 0.1),
                    ["eyeSquint"] = (float)(amplitude * 0.06),
                })),
                StyledFrame(context, t(1.32 * tempo), t(0.92 * tempo), EasingName.EaseInOut, Facs.ClampState(new Dictionary<string, float>
                {
                    ["headX"] = (float)(-side * amplitude * 0.06),
                    ["headY"] = (float)(-amplitude * 0.02),
                    ["headZ"] = (float)(-side * amplitude * 0.18 * tension),
                    ["bodyX"] = (float)(-side * amplitude * 0.08),
                    ["bodyY"] = (float)(-amplitude * 0.03),
                    ["bodyZ"] = (float)(side * amplitude * 0.14 * tension),
                    ["gazeX"] = (float)(side * amplitude * 0.08),
                    ["browDown"] = (float)(amplitude * 0.08),
                    ["eyeSquint"] = (float)(amplitude * 0.05),
                })),
                StyledFrame(context, t(2.58 * tempo), t(1.26 * tempo), EasingName.EaseInOut, Facs.ClampState(new Dictionary<string, float>
                {
                    ["headY"] = (float)(-amplitude * 0.03),
                    ["headZ"] = (float)(side * amplitude * 0.08),
                    ["bodyY"] = (float)(-amplitude * 0.03),
                    ["bodyZ"] = (float)(side * amplitude * 0.06),
                    ["gazeY"] = (float)(-amplitude * 0.02),
                    ["browDown"] = (float)(amplitude * 0.05),
                    ["eyeSquint"] = (float)(amplitude * 0.03),
                })),
                Frame(t(4.3), duration - t(4.3), EasingName.EaseOut, SwayRestFrame()),
            };
        }
    }
}
