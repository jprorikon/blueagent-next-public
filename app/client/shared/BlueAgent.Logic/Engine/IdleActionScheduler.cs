using System;
using System.Collections.Generic;

namespace BlueAgent.Logic.Engine
{
    /// <summary>待機小動作的種類（`wire` 是診斷／fixture 用的字串）。</summary>
    public enum IdleActionLabel
    {
        SmallNod,
        HeadTilt,
        SideLook,
        WeightShift,
        GentleLean,
        SighSink,
        SlowBlink,
    }

    /// <summary>`IdleActionUpdateContext`：一次 `Update` 的外部條件。</summary>
    public sealed class IdleActionContext
    {
        public readonly bool Enabled;
        public readonly double FocusLevel;
        public readonly VadRuntimeState Vad;
        public readonly ModelProfile Profile;
        public readonly CharacterPersonality Personality;
        public readonly bool Suppressed;

        public IdleActionContext(
            bool enabled,
            double focusLevel,
            VadRuntimeState vad,
            ModelProfile profile,
            CharacterPersonality personality = null,
            bool suppressed = false)
        {
            Enabled = enabled;
            FocusLevel = focusLevel;
            Vad = vad;
            Profile = profile;
            Personality = personality;
            Suppressed = suppressed;
        }
    }

    /// <summary>`IdleActionSchedulerState`（診斷／測試用）。</summary>
    public sealed class IdleActionState
    {
        public readonly string ActiveAction;
        public readonly int Direction;
        public readonly double? StartedAt;
        public readonly double Duration;
        public readonly double? NextActionAt;
        public readonly IReadOnlyList<string> RecentActions;
        public readonly IReadOnlyList<int> RecentDirections;
        public readonly bool Suppressed;

        public IdleActionState(
            string activeAction,
            int direction,
            double? startedAt,
            double duration,
            double? nextActionAt,
            IReadOnlyList<string> recentActions,
            IReadOnlyList<int> recentDirections,
            bool suppressed)
        {
            ActiveAction = activeAction;
            Direction = direction;
            StartedAt = startedAt;
            Duration = duration;
            NextActionAt = nextActionAt;
            RecentActions = recentActions;
            RecentDirections = recentDirections;
            Suppressed = suppressed;
        }
    }

    /// <summary>
    /// 待機小動作的排程與演出 —— C# 版，對照 Kotlin `idle/IdleActionScheduler.kt`。
    ///
    /// 跟微動／手勢的分工：
    ///  * 微動＝一直微微在動（連續）；
    ///  * 手勢＝VAD 變化觸發的動作；
    ///  * 這一支＝**沒有任何事件時**，每隔幾秒自己冒一個小動作（點頭、歪頭、看一眼旁邊、
    ///    換重心、往前傾、嘆氣下沉、慢慢眨眼）。
    ///
    /// 排程邏輯有兩層「避免重複」：最近 N 個沒用過的動作優先挑；方向也不能連續同向。
    /// 間隔時間用 `random^curve` 抽（曲線隨 spontaneity 與專注度變），所以不會變成節拍器。
    ///
    /// ⚠️ 亂數消耗次序（照抄）：`sampleInterval` 1 顆；`createAction` 依序＝加權挑選 1 顆
    ///    → 方向 1 顆（非方向性動作不抽）→ 時長 1 顆 → 振幅 1 顆。
    /// </summary>
    public sealed class IdleActionScheduler
    {
        private enum Label
        {
            SmallNod,
            HeadTilt,
            SideLook,
            WeightShift,
            GentleLean,
            SighSink,
            SlowBlink,
        }

        private sealed class PoseKeyframe
        {
            public readonly double Progress;
            public readonly IReadOnlyDictionary<string, float> Pose;

            public PoseKeyframe(double progress, IReadOnlyDictionary<string, float> pose)
            {
                Progress = progress;
                Pose = pose;
            }
        }

        private sealed class ActiveAction
        {
            public readonly Label ActionLabel;
            public readonly int Direction;
            public readonly double StartedAt;
            public readonly double Duration;
            public readonly IReadOnlyList<PoseKeyframe> Keyframes;

            public ActiveAction(
                Label label, int direction, double startedAt, double duration, IReadOnlyList<PoseKeyframe> keyframes)
            {
                ActionLabel = label;
                Direction = direction;
                StartedAt = startedAt;
                Duration = duration;
                Keyframes = keyframes;
            }
        }

        private sealed class Template
        {
            public readonly Label ActionLabel;
            public readonly double DurationMin;
            public readonly double DurationMax;
            public readonly bool Directional;
            public readonly Func<ModelCapabilities, bool> IsAvailable;

            public Template(
                Label label, double durationMin, double durationMax, bool directional, Func<ModelCapabilities, bool> isAvailable)
            {
                ActionLabel = label;
                DurationMin = durationMin;
                DurationMax = durationMax;
                Directional = directional;
                IsAvailable = isAvailable;
            }
        }

        private readonly double _seed;
        private readonly double _spontaneity;
        private readonly double _gain;
        private readonly double _minIntervalSeconds;
        private readonly double _maxIntervalSeconds;
        private readonly int _recentWindowSize;

        private RandomSource _random;
        private ActiveAction _active;
        private double? _nextActionAt;
        private List<Label> _recentActions = new List<Label>();
        private List<int> _recentDirections = new List<int>();
        private double _lastUpdateAt;
        private bool _suppressed;

        public IdleActionScheduler(
            double seed = 1.0,
            double? spontaneity = null,
            double? gain = null,
            double? minIntervalSeconds = null,
            double? maxIntervalSeconds = null,
            double? recentWindowSize = null)
        {
            _seed = FiniteOr(seed, 1.0);
            _spontaneity = EngineMath.Clamp(FiniteOr(spontaneity ?? 0.68, 0.68), 0.0, 1.0);
            _gain = EngineMath.Clamp(FiniteOr(gain ?? 1.0, 1.0), 0.0, 2.5);
            _minIntervalSeconds = EngineMath.Clamp(FiniteOr(minIntervalSeconds ?? 4.8, 4.8), 0.25, 120.0);
            _maxIntervalSeconds = EngineMath.Clamp(FiniteOr(maxIntervalSeconds ?? 11.5, 11.5), _minIntervalSeconds, 180.0);
            _recentWindowSize = (int)Math.Floor(EngineMath.Clamp(FiniteOr(recentWindowSize ?? 3.0, 3.0), 0.0, 6.0));
            _random = EngineMath.SeededRandom(_seed);
        }

        public Dictionary<string, float> Update(double timeSeconds, IdleActionContext context)
        {
            var time = Math.Max(0.0, FiniteOr(timeSeconds, _lastUpdateAt));
            if (time < _lastUpdateAt) Reset(time);
            _lastUpdateAt = time;

            if (!context.Enabled || context.Suppressed || _spontaneity <= 0 || _gain <= 0)
            {
                _active = null;
                _nextActionAt = null;
                _suppressed = true;
                return new Dictionary<string, float>();
            }

            var focusLevel = EngineMath.Clamp(FiniteOr(context.FocusLevel, 0.0), 0.0, 1.0);
            var nextAt = _nextActionAt;
            if (_suppressed || nextAt == null)
            {
                _suppressed = false;
                _nextActionAt = time + SampleInterval(focusLevel);
                return new Dictionary<string, float>();
            }

            var current = _active;
            if (current != null)
            {
                var elapsed = time - current.StartedAt;
                if (elapsed < current.Duration) return EvaluateAction(current, elapsed);
                _active = null;
            }

            if (time < nextAt.Value) return new Dictionary<string, float>();

            var action = CreateAction(time, context, focusLevel);
            if (action == null)
            {
                _nextActionAt = time + SampleInterval(focusLevel);
                return new Dictionary<string, float>();
            }

            _active = action;
            Remember(action.ActionLabel, action.Direction);
            _nextActionAt = time + action.Duration + SampleInterval(focusLevel);
            return EvaluateAction(action, 0.0);
        }

        public void Interrupt(double? timeSeconds = null)
        {
            var time = Math.Max(0.0, FiniteOr(timeSeconds ?? _lastUpdateAt, _lastUpdateAt));
            _active = null;
            _lastUpdateAt = time;
            _nextActionAt = time + SampleInterval(0.0);
        }

        public void Reset(double timeSeconds = 0.0)
        {
            _random = EngineMath.SeededRandom(_seed);
            _active = null;
            _nextActionAt = null;
            _recentActions = new List<Label>();
            _recentDirections = new List<int>();
            _lastUpdateAt = Math.Max(0.0, FiniteOr(timeSeconds, 0.0));
            _suppressed = false;
        }

        public IdleActionState GetState()
        {
            var recentActions = new List<string>();
            foreach (var label in _recentActions) recentActions.Add(Wire(label));
            return new IdleActionState(
                activeAction: _active == null ? null : Wire(_active.ActionLabel),
                direction: _active?.Direction ?? 0,
                startedAt: _active?.StartedAt,
                duration: _active?.Duration ?? 0.0,
                nextActionAt: _nextActionAt,
                recentActions: recentActions,
                recentDirections: new List<int>(_recentDirections),
                suppressed: _suppressed);
        }

        private ActiveAction CreateAction(double timeSeconds, IdleActionContext context, double focusLevel)
        {
            var capabilities = CapabilityDetector.ResolveCapabilities(context.Profile);
            var vad = ResolveVad(context.Vad);
            var personality = ResolvePersonality(context.Personality);
            var available = new List<Template>();
            foreach (var template in Templates)
            {
                if (template.IsAvailable(capabilities)) available.Add(template);
            }
            if (available.Count == 0) return null;

            var unrepeated = new List<Template>();
            if (_recentWindowSize > 0)
            {
                foreach (var template in available)
                {
                    if (!_recentActions.Contains(template.ActionLabel)) unrepeated.Add(template);
                }
            }
            else
            {
                unrepeated.AddRange(available);
            }

            var lastLabel = _recentActions.Count > 0 ? _recentActions[_recentActions.Count - 1] : (Label?)null;
            var candidates = new List<Template>();
            if (unrepeated.Count > 0)
            {
                candidates.AddRange(unrepeated);
            }
            else
            {
                foreach (var template in available)
                {
                    if (lastLabel == null || template.ActionLabel != lastLabel.Value) candidates.Add(template);
                }
            }
            var pool = candidates.Count > 0 ? candidates : available;

            var picked = WeightedPick(pool, t => ActionWeight(t.ActionLabel, vad, personality, focusLevel), _random);
            var direction = picked.Directional ? PickDirection() : 0;
            var duration = picked.DurationMin + (picked.DurationMax - picked.DurationMin) * _random.Next();
            var vadIntensity = (Math.Abs(vad.Valence) + Math.Abs(vad.Arousal) * 0.82 + Math.Abs(vad.Dominance) * 0.64) / 2.46;
            var amplitude = EngineMath.Clamp(
                _gain *
                (0.72 + personality.Expressiveness * 0.4) *
                (1 - focusLevel * 0.38) *
                (0.9 + _spontaneity * 0.14) *
                (0.9 + vadIntensity * 0.18) *
                (0.86 + _random.Next() * 0.28),
                0.0,
                2.2);

            return new ActiveAction(
                label: picked.ActionLabel,
                direction: direction,
                startedAt: timeSeconds,
                duration: duration,
                keyframes: BuildKeyframes(picked.ActionLabel, direction, amplitude, capabilities));
        }

        private int PickDirection()
        {
            var direction = _random.Next() < 0.5 ? -1 : 1;
            var lastDirection = _recentDirections.Count > 0 ? _recentDirections[_recentDirections.Count - 1] : (int?)null;
            if (_recentWindowSize > 0 && lastDirection != null && direction == lastDirection.Value)
            {
                direction = direction == -1 ? 1 : -1;
            }
            return direction;
        }

        private void Remember(Label label, int direction)
        {
            if (_recentWindowSize <= 0) return;
            _recentActions.Add(label);
            while (_recentActions.Count > _recentWindowSize) _recentActions.RemoveAt(0);
            if (direction != 0)
            {
                _recentDirections.Add(direction);
                while (_recentDirections.Count > _recentWindowSize) _recentDirections.RemoveAt(0);
            }
        }

        private double SampleInterval(double focusLevel)
        {
            var curve = EngineMath.Clamp(0.68 + _spontaneity * 1.7 - focusLevel * 0.32, 0.42, 2.38);
            var randomPosition = Math.Pow(_random.Next(), curve);
            var focusAdjusted = randomPosition + (1 - randomPosition) * focusLevel * 0.34;
            return _minIntervalSeconds + (_maxIntervalSeconds - _minIntervalSeconds) * focusAdjusted;
        }

        private static double FiniteOr(double? value, double fallback) =>
            value.HasValue && double.IsFinite(value.Value) ? value.Value : fallback;

        // ── 模板表（順序照抄：加權挑選的平手取捨靠它）────────────────────────────

        private static readonly Template[] Templates =
        {
            new Template(Label.SmallNod, 0.82, 1.2, false, c => c.HeadControl || c.BodyControl),
            new Template(Label.HeadTilt, 1.35, 2.15, true, c => c.HeadControl || c.GazeControl),
            new Template(Label.SideLook, 1.45, 2.35, true, c => c.GazeControl || c.HeadControl),
            new Template(Label.WeightShift, 1.65, 2.65, true, c => c.BodyControl || c.HeadControl),
            new Template(Label.GentleLean, 1.25, 2.05, true, c => c.BodyControl || c.HeadControl),
            new Template(Label.SighSink, 1.7, 2.8, false,
                c => c.BodyControl || c.HeadControl || c.GazeControl || c.BrowControl || c.EyeBlink),
            new Template(Label.SlowBlink, 0.72, 1.08, true, c => c.EyeBlink),
        };

        private static string Wire(Label label)
        {
            switch (label)
            {
                case Label.SmallNod: return "small-nod";
                case Label.HeadTilt: return "head-tilt";
                case Label.SideLook: return "side-look";
                case Label.WeightShift: return "weight-shift";
                case Label.GentleLean: return "gentle-lean";
                case Label.SighSink: return "sigh-sink";
                default: return "slow-blink";
            }
        }

        private static VadVector ResolveVad(VadRuntimeState input)
        {
            var source = input.Current;
            return new VadVector(
                EngineMath.Clamp(FiniteOr(source.Valence, 0.0), -1.0, 1.0),
                EngineMath.Clamp(FiniteOr(source.Arousal, 0.0), -1.0, 1.0),
                EngineMath.Clamp(FiniteOr(source.Dominance, 0.0), -1.0, 1.0));
        }

        private static CharacterPersonality ResolvePersonality(CharacterPersonality input)
        {
            var dflt = new CharacterPersonality();
            return new CharacterPersonality(
                expressiveness: EngineMath.Clamp(FiniteOr(input?.Expressiveness, dflt.Expressiveness), 0.0, 1.0),
                softness: EngineMath.Clamp(FiniteOr(input?.Softness, dflt.Softness), 0.0, 1.0),
                shyness: EngineMath.Clamp(FiniteOr(input?.Shyness, dflt.Shyness), 0.0, 1.0),
                gazeStability: EngineMath.Clamp(FiniteOr(input?.GazeStability, dflt.GazeStability), 0.0, 1.0));
        }

        private static double ActionWeight(Label label, VadVector vad, CharacterPersonality personality, double focusLevel)
        {
            var positive = Math.Max(0.0, vad.Valence);
            var negative = Math.Max(0.0, -vad.Valence);
            var aroused = Math.Max(0.0, vad.Arousal);
            var calm = Math.Max(0.0, -vad.Arousal);
            var dominant = Math.Max(0.0, vad.Dominance);
            var submissive = Math.Max(0.0, -vad.Dominance);

            switch (label)
            {
                case Label.SmallNod:
                    return 1.05 + positive * 0.42 + aroused * 0.4 + focusLevel * 0.42 + personality.Expressiveness * 0.28;
                case Label.HeadTilt:
                    return 0.92 + positive * 0.18 + submissive * 0.24 + personality.Softness * 0.34 + personality.Shyness * 0.16;
                case Label.SideLook:
                    return 0.62 + negative * 0.28 + submissive * 0.36 + personality.Shyness * 0.56 + (1 - personality.GazeStability) * 0.6;
                case Label.WeightShift:
                    return 0.86 + aroused * 0.24 + dominant * 0.34 + personality.Expressiveness * 0.34;
                case Label.GentleLean:
                    return 0.76 + positive * 0.34 + dominant * 0.28 + focusLevel * 0.18 + personality.Expressiveness * 0.32 - personality.Shyness * 0.12;
                case Label.SighSink:
                    return 0.48 + negative * 0.62 + calm * 0.52 + personality.Softness * 0.32;
                default:
                    return 0.72 + calm * 0.56 + personality.Softness * 0.38 + focusLevel * 0.12;
            }
        }

        private static Template WeightedPick(
            IReadOnlyList<Template> templates, Func<Template, double> getWeight, RandomSource random)
        {
            var weights = new double[templates.Count];
            var total = 0.0;
            for (var i = 0; i < templates.Count; i++)
            {
                weights[i] = Math.Max(0.001, getWeight(templates[i]));
                total += weights[i];
            }
            var cursor = random.Next() * total;
            for (var i = 0; i < templates.Count; i++)
            {
                cursor -= weights[i];
                if (cursor <= 0) return templates[i];
            }
            return templates[templates.Count - 1];
        }

        // ── 關鍵影格 ────────────────────────────────────────────────────────

        private static IReadOnlyList<PoseKeyframe> BuildKeyframes(
            Label label, int direction, double amplitude, ModelCapabilities capabilities)
        {
            // ⚠️ `side`：非方向性動作（direction 0）也會用到，此時當 +1
            var side = direction == 0 ? 1 : direction;
            List<PoseKeyframe> frames;

            switch (label)
            {
                case Label.SmallNod:
                    frames = new List<PoseKeyframe>
                    {
                        Frame(0.0),
                        Frame(0.2, new Dictionary<string, double> { { "headY", 0.086 }, { "bodyY", 0.018 }, { "gazeY", 0.012 } }),
                        Frame(0.42, new Dictionary<string, double> { { "headY", -0.026 }, { "bodyY", -0.006 }, { "gazeY", -0.008 } }),
                        Frame(0.68, new Dictionary<string, double> { { "headY", 0.016 }, { "bodyY", 0.004 } }),
                        Frame(1.0),
                    };
                    break;
                case Label.HeadTilt:
                    frames = new List<PoseKeyframe>
                    {
                        Frame(0.0),
                        Frame(0.28, new Dictionary<string, double>
                        {
                            { "headX", side * 0.018 }, { "headZ", side * 0.094 },
                            { "gazeX", -side * 0.038 }, { "browOuterUp", 0.024 },
                        }),
                        Frame(0.64, new Dictionary<string, double>
                        {
                            { "headX", side * 0.014 }, { "headZ", side * 0.078 },
                            { "gazeX", -side * 0.025 }, { "browOuterUp", 0.016 },
                        }),
                        Frame(1.0),
                    };
                    break;
                case Label.SideLook:
                    frames = new List<PoseKeyframe>
                    {
                        Frame(0.0),
                        Frame(0.18, new Dictionary<string, double> { { "gazeX", side * 0.18 }, { "gazeY", 0.018 } }),
                        Frame(0.38, new Dictionary<string, double>
                        {
                            { "gazeX", side * 0.23 }, { "gazeY", 0.012 },
                            { "headX", side * 0.046 }, { "headZ", -side * 0.026 },
                        }),
                        Frame(0.62, new Dictionary<string, double>
                        {
                            { "gazeX", side * 0.2 }, { "headX", side * 0.04 }, { "headZ", -side * 0.022 },
                        }),
                        Frame(0.8, new Dictionary<string, double>
                        {
                            { "gazeX", side * 0.028 }, { "headX", side * 0.034 }, { "headZ", -side * 0.018 },
                        }),
                        Frame(1.0),
                    };
                    break;
                case Label.WeightShift:
                    frames = new List<PoseKeyframe>
                    {
                        Frame(0.0),
                        Frame(0.34, new Dictionary<string, double>
                        {
                            { "bodyX", side * 0.078 }, { "bodyZ", side * 0.055 },
                            { "headX", -side * 0.018 }, { "headZ", -side * 0.036 }, { "gazeX", -side * 0.026 },
                        }),
                        Frame(0.7, new Dictionary<string, double>
                        {
                            { "bodyX", side * 0.066 }, { "bodyZ", side * 0.046 },
                            { "headX", -side * 0.014 }, { "headZ", -side * 0.03 }, { "gazeX", -side * 0.018 },
                        }),
                        Frame(1.0),
                    };
                    break;
                case Label.GentleLean:
                    frames = new List<PoseKeyframe>
                    {
                        Frame(0.0),
                        Frame(0.3, new Dictionary<string, double>
                        {
                            { "bodyY", side * 0.074 }, { "headY", side * 0.044 }, { "gazeY", side * 0.022 },
                            { "browOuterUp", side > 0 ? 0.018 : 0.0 },
                        }),
                        Frame(0.58, new Dictionary<string, double>
                        {
                            { "bodyY", side * 0.064 }, { "headY", side * 0.038 }, { "gazeY", side * 0.018 },
                            { "browOuterUp", side > 0 ? 0.012 : 0.0 },
                        }),
                        Frame(0.8, new Dictionary<string, double> { { "bodyY", -side * 0.012 }, { "headY", -side * 0.008 } }),
                        Frame(1.0),
                    };
                    break;
                case Label.SighSink:
                    frames = new List<PoseKeyframe>
                    {
                        Frame(0.0),
                        Frame(0.2, new Dictionary<string, double> { { "browInnerUp", 0.018 }, { "eyeBlinkL", 0.08 }, { "eyeBlinkR", 0.09 } }),
                        Frame(0.48, new Dictionary<string, double>
                        {
                            { "headY", -0.064 }, { "bodyY", -0.054 }, { "gazeY", -0.048 },
                            { "browInnerUp", 0.026 }, { "eyeBlinkL", 0.2 }, { "eyeBlinkR", 0.22 },
                        }),
                        Frame(0.73, new Dictionary<string, double>
                        {
                            { "headY", -0.048 }, { "bodyY", -0.042 }, { "gazeY", -0.034 },
                            { "browInnerUp", 0.018 }, { "eyeBlinkL", 0.06 }, { "eyeBlinkR", 0.07 },
                        }),
                        Frame(1.0),
                    };
                    break;
                default:
                    var leftPeak = side < 0 ? 0.94 : 0.82;
                    var rightPeak = side > 0 ? 0.94 : 0.82;
                    frames = new List<PoseKeyframe>
                    {
                        Frame(0.0),
                        Frame(0.3, new Dictionary<string, double>
                        {
                            { "eyeBlinkL", leftPeak * 0.86 }, { "eyeBlinkR", rightPeak * 0.82 }, { "headY", -0.01 },
                        }),
                        Frame(0.47, new Dictionary<string, double>
                        {
                            { "eyeBlinkL", leftPeak }, { "eyeBlinkR", rightPeak }, { "headY", -0.014 },
                        }),
                        Frame(0.72, new Dictionary<string, double>
                        {
                            { "eyeBlinkL", 0.2 }, { "eyeBlinkR", 0.18 }, { "headY", -0.006 },
                        }),
                        Frame(1.0),
                    };
                    break;
            }

            var outp = new List<PoseKeyframe>(frames.Count);
            foreach (var kf in frames) outp.Add(new PoseKeyframe(kf.Progress, ScaleAndFilterPose(kf.Pose, amplitude, capabilities)));
            return outp;
        }

        private static PoseKeyframe Frame(double progress, IReadOnlyDictionary<string, double> pose = null)
        {
            var converted = new Dictionary<string, float>();
            if (pose != null)
            {
                foreach (var kv in pose) converted[kv.Key] = (float)kv.Value;
            }
            return new PoseKeyframe(progress, converted);
        }

        private static Dictionary<string, float> ScaleAndFilterPose(
            IReadOnlyDictionary<string, float> pose, double scale, ModelCapabilities capabilities)
        {
            var outp = new Dictionary<string, float>();
            foreach (var kv in pose)
            {
                if (!SupportsKey(kv.Key, capabilities)) continue;
                outp[kv.Key] = (float)(kv.Value * scale);
            }
            return Facs.ClampState(outp);
        }

        private static bool SupportsKey(string key, ModelCapabilities c)
        {
            switch (key)
            {
                case "headX":
                case "headY":
                case "headZ": return c.HeadControl;
                case "bodyX":
                case "bodyY":
                case "bodyZ": return c.BodyControl;
                case "gazeX":
                case "gazeY": return c.GazeControl;
                case "eyeBlinkL":
                case "eyeBlinkR": return c.EyeBlink;
                case "eyeSmile": return c.EyeSmile;
                case "browInnerUp":
                case "browOuterUp":
                case "browDown": return c.BrowControl;
                case "mouthOpen":
                case "mouthPucker": return c.MouthOpen;
                case "mouthSmile":
                case "mouthFrown": return c.MouthSmile;
                case "blush": return c.Blush;
                case "tear": return c.Tear;
                case "sweat": return c.Sweat;
                case "breath": return c.Breath;
                default: return true;
            }
        }

        /// <summary>⚠️ 這裡用的是 **smoothstep**（`local²(3-2local)`），不是 easing 表裡那幾種。</summary>
        private static Dictionary<string, float> EvaluateAction(ActiveAction action, double elapsedSeconds)
        {
            var progress = EngineMath.Clamp(elapsedSeconds / action.Duration, 0.0, 1.0);
            var from = action.Keyframes[0];
            var to = action.Keyframes[action.Keyframes.Count - 1];

            for (var index = 1; index < action.Keyframes.Count; index++)
            {
                var candidate = action.Keyframes[index];
                if (progress <= candidate.Progress)
                {
                    to = candidate;
                    from = action.Keyframes[index - 1];
                    break;
                }
            }

            var span = to.Progress - from.Progress;
            var local = span <= 0 ? 1.0 : EngineMath.Clamp((progress - from.Progress) / span, 0.0, 1.0);
            var eased = local * local * (3 - 2 * local);

            // 鍵序＝「from 的鍵，接上 to 新增的鍵」（Kotlin 的 LinkedHashSet）
            var keys = new List<string>();
            foreach (var key in from.Pose.Keys)
            {
                if (!keys.Contains(key)) keys.Add(key);
            }
            foreach (var key in to.Pose.Keys)
            {
                if (!keys.Contains(key)) keys.Add(key);
            }

            var outp = new Dictionary<string, float>();
            foreach (var key in keys)
            {
                var start = from.Pose.TryGetValue(key, out var s) ? (double)s : 0.0;
                var end = to.Pose.TryGetValue(key, out var e) ? (double)e : 0.0;
                outp[key] = (float)(start + (end - start) * eased);
            }

            return Facs.ClampState(outp);
        }
    }
}
