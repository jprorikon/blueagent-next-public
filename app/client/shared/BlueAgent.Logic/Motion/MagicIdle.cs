using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace BlueAgent.Logic.Motion
{
    /// <summary>
    /// 待機動作的訓練資料檔（`magic-train/1`，`Resources/models3d/magic-idle.json`）。
    ///
    /// 帶的是**訓練序列**，不是擬合好的模型：7 軸 × 幾千幀的 VAR 擬合是一個 85×85 的正規方程，
    /// 手機上幾毫秒；比把係數／殘差／訓練幀全序列化簡單，而且同一份資料兩端擬合出來一定一樣
    /// （`docs/PLAN-magic-motion-20261031.md` §6.2）。
    ///
    /// ```json
    /// {"format":"magic-train/1","sampleRateHz":30,"axes":["bodyX",…],"source":"…","frames":[[…],…]}
    /// ```
    /// 每一欄是**角度（度）**，繞角色自己的軸：X＝右（前後彎）、Y＝上（左右轉）、Z＝前（左右歪）。
    /// </summary>
    public sealed class MagicIdleData
    {
        public const string Format = "magic-train/1";

        public string[] Axes;
        public string Source = "";
        public MagicTrainingSequence Sequence;

        public static MagicIdleData Parse(string json)
        {
            var root = new JObject(json);
            if (root.OptString("format") != Format)
                throw new FormatException("not a " + Format + " file: " + root.OptString("format"));
            var axesArray = root.OptJArray("axes");
            var framesArray = root.OptJArray("frames");
            if (axesArray == null || framesArray == null || axesArray.Length() == 0)
                throw new FormatException("missing axes or frames");

            var axes = new string[axesArray.Length()];
            for (var i = 0; i < axes.Length; i++) axes[i] = axesArray.OptString(i);
            var frames = new List<double[]>(framesArray.Length());
            for (var f = 0; f < framesArray.Length(); f++)
            {
                if (!(framesArray.Opt(f) is JArray row) || row.Length() != axes.Length)
                    throw new FormatException("frame " + f + " does not have " + axes.Length + " values");
                var frame = new double[axes.Length];
                for (var a = 0; a < axes.Length; a++) frame[a] = Number(row.Opt(a));
                frames.Add(frame);
            }
            var rate = root.OptDouble("sampleRateHz", 30);
            return new MagicIdleData
            {
                Axes = axes,
                Source = root.OptString("source"),
                Sequence = new MagicTrainingSequence
                {
                    SampleRateHz = rate,
                    SourceDurationMs = frames.Count > 0 ? (frames.Count - 1) / rate * 1000 : 0,
                    Frames = frames,
                },
            };
        }

        private static double Number(object value)
        {
            switch (value)
            {
                case int i: return i;
                case long l: return l;
                case double d: return d;
                case string s when double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out var parsed):
                    return parsed;
                default: throw new FormatException("not a number: " + value);
            }
        }

        public string ToJson()
        {
            var text = new StringBuilder();
            text.Append("{\"format\":\"").Append(Format).Append("\",\"sampleRateHz\":")
                .Append(Sequence.SampleRateHz.ToString("R", CultureInfo.InvariantCulture)).Append(",\"axes\":[");
            for (var i = 0; i < Axes.Length; i++) text.Append(i > 0 ? "," : "").Append('"').Append(Axes[i]).Append('"');
            text.Append("],\"source\":\"").Append((Source ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"")).Append("\",\"frames\":[");
            for (var f = 0; f < Sequence.Frames.Count; f++)
            {
                text.Append(f > 0 ? ",[" : "[");
                var frame = Sequence.Frames[f];
                for (var a = 0; a < frame.Length; a++)
                    text.Append(a > 0 ? "," : "").Append(frame[a].ToString("R", CultureInfo.InvariantCulture));
                text.Append(']');
            }
            return text.Append("]}").ToString();
        }
    }

    /// <summary>
    /// 把 VAR 生成器變成「每個渲染幀問一次」的待機姿勢：
    ///
    /// - **取樣率**：生成器一次 `Next()` 是一個 <see cref="VarGenerator.SampleRateHz"/> 的幀；這裡按真實時間累加，
    ///   兩個生成幀之間線性插值 ⇒ 60 fps 渲染、30 Hz 模型也是原速（不能每個渲染幀叫一次 `Next()`）。
    /// - **置中**：回的是「減掉訓練均值」的偏移量（度），疊在靜止姿勢上；訓練資料整體前傾幾度不會變成她一直前傾。
    /// - **大跳**（App 切回前景、dt 很大）：最多補幾步就對齊，不一次生成幾百幀。
    /// </summary>
    public sealed class MagicIdlePlayer
    {
        private const int MaxCatchUpSteps = 4;

        private readonly IMagicIdleSource _generator;
        private readonly double[] _mean;
        private readonly Dictionary<string, int> _axes = new Dictionary<string, int>();
        private double[] _previous;
        private double[] _next;
        private double _phase;
        /// <summary>慣性化殘餘偏移（每軸，度）：換狀態時把「跳變量」記在這裡，再逐幀衰減。</summary>
        private double[] _offset;
        private int _lastState = -1;

        public double NoiseScale { get; set; }

        /// <summary>
        /// 每軸每秒最多走幾度（0＝不限）。AR-HMM 換狀態時會突然跳到另一個姿勢，
        /// 把這個打開就變成「快速滑過去」而不是「瞬移」——owner 2026-10-31 回報的「動作跳躍」。
        /// 生成端不動，只在播放端限速，所以 VAR 也可以共用。
        /// </summary>
        public double MaxDegreesPerSecond { get; set; }

        /// <summary>
        /// 每軸偏移的絕對上限（度；0＝不限）。AR-HMM 偶爾會走到訓練資料的邊緣（例如 ±50°），
        /// 待機看起來就是「忽然換姿勢」；夾在十幾度內（端末用 12°）就只剩小幅晃動（owner 2026-10-31）。
        /// </summary>
        public double MaxOffsetDegrees { get; set; }

        /// <summary>
        /// 慣性化時間常數（秒；0＝關）。偵測到生成器換狀態時，把「目前顯示的姿勢」與新軌跡的差
        /// 記成一個偏移，之後逐幀指數衰減 ⇒ 畫面上是**從舊姿勢滑進新姿勢**，
        /// 而不是瞬移（owner 2026-10-31 第 3 案：AR-HMM 只拿它的幅度，過渡不跳）。
        /// 跟限速的差別：限速是硬夾（會拖、幅度被壓），慣性化只在換狀態那一下作用，長期完全跟隨目標。
        /// </summary>
        public double TransitionSeconds { get; set; }

        /// <summary>VAR 驅動（預設）：一組動態、平滑，對「不換姿勢」的待機最穩。</summary>
        public MagicIdlePlayer(VarModel model, IReadOnlyList<string> axes, uint seed, double noiseScale = 1.0)
            : this(model == null
                       ? throw new ArgumentNullException(nameof(model))
                       : (IMagicIdleSource)new VarIdleSource(model.ToGenerator(seed)),
                   model == null ? throw new ArgumentNullException(nameof(model)) : model.Mean,
                   axes, noiseScale)
        {
        }

        /// <summary>AR-HMM 驅動（實驗）：會在動作基元之間跳，換姿勢比較明顯。</summary>
        public MagicIdlePlayer(ArHmmModel model, IReadOnlyList<string> axes, uint seed, double noiseScale = 1.0)
            : this(model == null
                       ? throw new ArgumentNullException(nameof(model))
                       : (IMagicIdleSource)new ArHmmIdleSource(model.ToGenerator(seed)),
                   model == null ? throw new ArgumentNullException(nameof(model)) : model.Mean,
                   axes, noiseScale)
        {
        }

        /// <summary>用任何 <see cref="IMagicIdleSource"/> 建播放器（測試與自訂來源用）。</summary>
        public MagicIdlePlayer(IMagicIdleSource generator, double[] mean, IReadOnlyList<string> axes,
                               double noiseScale = 1.0)
        {
            _generator = generator;
            _mean = mean;
            for (var i = 0; i < axes.Count; i++) _axes[axes[i]] = i;
            NoiseScale = noiseScale;
            _previous = _generator.Next(NoiseScale);
            _next = _generator.Next(NoiseScale);
            _offset = new double[_next.Length];
        }

        /// <summary>往前走 <paramref name="seconds"/> 秒（真實時間）。</summary>
        public void Advance(double seconds)
        {
            if (seconds <= 0 || double.IsNaN(seconds)) return;
            _phase += seconds * _generator.SampleRateHz;
            var steps = 0;
            while (_phase >= 1.0)
            {
                _phase -= 1.0;
                if (++steps > MaxCatchUpSteps)
                {
                    _phase %= 1.0;
                    break;
                }
                _previous = _next;
                var raw = Limited(_previous, _generator.Next(NoiseScale));
                CaptureTransition(raw);
                DecayOffset();
                _next = raw;
            }
        }

        /// <summary>偏移逐幀指數衰減（時間常數 <see cref="TransitionSeconds"/>）。</summary>
        private void DecayOffset()
        {
            if (TransitionSeconds <= 0 || _offset == null) return;
            var decay = Math.Exp(-1.0 / Math.Max(1e-6, TransitionSeconds * _generator.SampleRateHz));
            for (var index = 0; index < _offset.Length; index++) _offset[index] *= decay;
        }

        /// <summary>生成器換了隱狀態 ⇒ 把「現在顯示的姿勢」與新軌跡的差記成偏移（之後衰減）。</summary>
        private void CaptureTransition(double[] raw)
        {
            if (TransitionSeconds <= 0) return;
            if (!(_generator is IMagicIdleStateful stateful)) return;
            var state = stateful.State;
            if (_lastState < 0)
            {
                _lastState = state;
                return;
            }
            if (state == _lastState) return;
            _lastState = state;
            if (_offset == null || _offset.Length != raw.Length) _offset = new double[raw.Length];
            for (var index = 0; index < raw.Length; index++)
            {
                // 舊姿勢（含既有偏移）減掉新軌跡 ⇒ 這一幀照樣顯示舊姿勢，之後平滑收斂
                var shown = (_previous != null && index < _previous.Length ? _previous[index] : 0.0)
                            + _offset[index];
                _offset[index] = shown - raw[index];
            }
            // 換軌：插值基準也要換成新軌跡的第一幀，否則跳變只是被推遲一幀（實測會更糟）
            _previous = (double[])raw.Clone();
        }

        /// <summary>把「這一幀到下一幀的跳躍」壓到每秒上限之內（0＝不限）。</summary>
        private double[] Limited(double[] previous, double[] next)
        {
            var limit = MaxDegreesPerSecond;
            if (limit <= 0 || previous == null || next == null) return next;
            var step = limit / Math.Max(1e-6, _generator.SampleRateHz);
            var limited = new double[next.Length];
            for (var index = 0; index < next.Length; index++)
            {
                var delta = next[index] - previous[index];
                if (delta > step) delta = step;
                else if (delta < -step) delta = -step;
                limited[index] = previous[index] + delta;
            }
            return limited;
        }

        /// <summary>某一軸現在的偏移（度，已減掉訓練均值）；沒有這一軸 ⇒ 0。</summary>
        public double Offset(string axis)
        {
            if (!_axes.TryGetValue(axis, out var index)) return 0;
            var value = _previous[index] + (_next[index] - _previous[index]) * _phase;
            if (TransitionSeconds > 0 && _offset != null && index < _offset.Length) value += _offset[index];
            var offset = value - _mean[index];
            var limit = MaxOffsetDegrees;
            if (limit > 0)
            {
                if (offset > limit) offset = limit;
                else if (offset < -limit) offset = -limit;
            }
            return offset;
        }

        public bool Has(string axis) => _axes.ContainsKey(axis);
    }

    /// <summary>待機生成來源：VAR 與 AR-HMM 共用這個介面（一次給一幀，原尺度、全寬）。</summary>
    public interface IMagicIdleSource
    {
        /// <summary>一次 `Next()` 前進的是這個取樣率的一幀。</summary>
        double SampleRateHz { get; }
        double[] Next(double noiseScale);
    }

    /// <summary>會回報隱狀態的來源（AR-HMM）：播放器靠它偵測「換姿勢」。</summary>
    public interface IMagicIdleStateful
    {
        int State { get; }
    }

    /// <summary>VAR 來源（<see cref="VarGenerator"/> 的轉接）。</summary>
    public sealed class VarIdleSource : IMagicIdleSource
    {
        private readonly VarGenerator _generator;

        public VarIdleSource(VarGenerator generator) =>
            _generator = generator ?? throw new ArgumentNullException(nameof(generator));

        public double SampleRateHz => _generator.SampleRateHz;
        public double[] Next(double noiseScale) => _generator.Next(noiseScale);
    }

    /// <summary>AR-HMM 來源（<see cref="ArHmmGenerator"/> 的轉接；隱狀態只是不往外傳）。</summary>
    public sealed class ArHmmIdleSource : IMagicIdleSource, IMagicIdleStateful
    {
        private readonly ArHmmGenerator _generator;

        public ArHmmIdleSource(ArHmmGenerator generator) =>
            _generator = generator ?? throw new ArgumentNullException(nameof(generator));

        public double SampleRateHz => _generator.SampleRateHz;
        /// <summary>上一次 `Next()` 落在哪個隱狀態。</summary>
        public int State => _generator.State;
        public double[] Next(double noiseScale) => _generator.Next(noiseScale).Values;
    }
}
