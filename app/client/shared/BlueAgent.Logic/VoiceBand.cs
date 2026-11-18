using System;

namespace BlueAgent.Logic
{
    /// <summary>
    /// Voice-band filtering, applied to the microphone stream BEFORE the gates and before whisper
    /// ——**Kotlin `VoiceBand.kt` 的逐行移植**（真值＝`blueagent/live2d/android/kmp` 那一份；
    /// 等價性由 `fixtures/voice_band.json` 逐條對拍）。
    ///
    /// WHY: an open microphone on a desk does not hear "speech plus a little noise" — it hears speech
    /// buried in the room. A desk thump, an extractor fan, a lorry outside and the phone's own handling
    /// noise are all energy at 20–150 Hz, and that energy is exactly what an RMS/peak gate mistakes for a
    /// voice: it lifts the floor, opens the recorder on a door slam, and hands whisper a clip whose loudest
    /// content is rumble. Filtering first means the floor, the peak gate and the transcription all judge
    /// the same signal — the human one.
    ///
    /// WHAT BAND: 100 Hz – 7.2 kHz at 16 kHz sampling.
    ///  * The low cut is where the value is, and it is ordinary practice (telephony cuts at 300 Hz;
    ///    whisper's own preprocessing assumes nothing below ~100 Hz matters for speech): desk thumps,
    ///    handling noise, fan rumble and DC offsets are all energy the gates mistake for a voice.
    ///  * The high cut is deliberately gentle, and the honest reason is arithmetic: at 16 kHz sampling
    ///    there is NOTHING above 8 kHz to remove, so a low-pass can only shape the top of what speech
    ///    already uses. Cutting at the telephone band (3400 Hz) would take the 4–8 kHz energy that
    ///    Japanese needs for サ/シ/ス/セ/ソ and cost accuracy; cutting at ~7.2 kHz trims the harsh top of
    ///    the hiss by about 1 dB at 5 kHz and 2 dB at 6.5 kHz, which is a real but small improvement.
    ///    (Measured, not guessed: see `VoiceBandTest`. A tone at 11 kHz is not testable at all here — it
    ///    aliases below Nyquist.)
    ///
    /// Two RBJ biquads in series, Q = 1/√2 (Butterworth, no peaking), 12 dB/octave each. Streaming: the
    /// state carries across blocks, so a 30 ms block boundary is not a discontinuity.
    ///
    /// `enabled = false` makes <see cref="Process(short[])"/> a no-op, which is how the setting is honoured
    /// (and how the unfiltered baseline is measured).
    ///
    /// ⚠️ `Process`（16-bit 值域就地寫回）與 <see cref="Step"/>（逐樣本 Double）**共用同一份 biquad 狀態**
    /// ——Kotlin 就是這樣（iOS 那條鏈把量化往後挪，濾波器本身兩端共用同一份）。所以同一個物件上
    /// 兩種入口混著叫時，狀態是延續的，不要以為它們各有一份。
    /// </summary>
    public sealed class VoiceBand
    {
        public const int SAMPLE_RATE = 16000;
        public const double HIGH_PASS_HZ = 100.0;

        /// <summary>Just inside the 0.45 ceiling: at 16 kHz there is nothing above 8 kHz anyway.</summary>
        public const double LOW_PASS_HZ = 7200.0;

        private const double SCALE = 32768.0;

        private readonly bool _enabled;
        private readonly Biquad _high;
        private readonly Biquad _low;

        /// <summary>
        /// 建構子（Kotlin 的 primary constructor ＋ 無參建構子收成一支，C# 的預設參數就夠）。
        /// 截止頻率**先夾界再算係數**：高通夾在 `[20, sampleRate×0.45]`、低通夾在 `[200, sampleRate×0.45]`
        /// （`sampleRate × 0.45` 之上沒有東西可留，0.45 也還在 biquad 撐得住的地方）。
        /// </summary>
        public VoiceBand(int sampleRate = SAMPLE_RATE, double highPassHz = HIGH_PASS_HZ,
                         double lowPassHz = LOW_PASS_HZ, bool enabled = true)
        {
            _enabled = enabled;
            var highCut = CoerceIn(highPassHz, 20.0, sampleRate * 0.45);
            var lowCut = CoerceIn(lowPassHz, 200.0, sampleRate * 0.45);
            _high = Biquad.HighPass(sampleRate, highCut);
            _low = Biquad.LowPass(sampleRate, lowCut);
        }

        /// <summary>`enabled`（＝Kotlin 的 `isActive`）。</summary>
        public bool IsActive => _enabled;

        /// <summary>Filter the whole array in place. 16-bit PCM in, 16-bit PCM out.</summary>
        public void Process(short[] samples) => Process(samples, samples.Length);

        /// <summary>
        /// Filter <paramref name="count"/> samples of <paramref name="samples"/> in place.
        /// 16-bit PCM in, 16-bit PCM out.（Kotlin 的 `count` 預設＝`samples.size`，C# 用上一個多載代替。）
        /// </summary>
        public void Process(short[] samples, int count)
        {
            if (!_enabled) return;
            for (var i = 0; i < count; i++)
            {
                // 與 Kotlin 逐字相同：先除以刻度、兩顆 biquad 串起來、乘回刻度、夾界、往零截斷成 int16。
                var outv = _low.Step(_high.Step(samples[i] / SCALE));
                samples[i] = (short)(int)CoerceIn(outv * SCALE, -SCALE, SCALE - 1);
            }
        }

        public void Reset()
        {
            _high.Reset();
            _low.Reset();
        }

        /// <summary>
        /// 逐樣本、**Double 值域**的一步（iOS 的 `MicCapture` 用這一支）。
        ///
        /// 為什麼不能讓它用 <see cref="Process(short[])"/>：那一支在 16-bit 值域就寫回去，而 iOS 的鏈是
        /// 「帶通 → **逐塊 AGC** → 夾住」——AGC 要在**量化之前**、在 Double 上做（固定增益顧得了安靜就顧不了電視）。
        /// 所以把量化往後挪的責任落在呼叫端，濾波器本身兩端共用同一份：這裡＝ <see cref="Process(short[],int)"/>
        /// 裡面那行一模一樣的算式。
        /// </summary>
        public double Step(double x) => !_enabled ? x : _low.Step(_high.Step(x));

        /// <summary>
        /// One-shot use (a wav loaded for a diagnostic run): a fresh filter, no state to carry.
        /// 回傳的就是傳進來的那一個陣列（Kotlin 也是回 `pcm`——關掉時**原樣**回同一個物件）。
        /// </summary>
        public short[] ApplyTo(short[] pcm)
        {
            if (!_enabled) return pcm;
            Process(pcm);
            return pcm;
        }

        /// <summary>`coerceIn`——比預設值小的夾到 min、比 max 大的夾到 max。</summary>
        private static double CoerceIn(double v, double min, double max) => v < min ? min : (v > max ? max : v);

        /// <summary>A single second-order section, direct form 1 transposed.</summary>
        public sealed class Biquad
        {
            /// <summary>1/√2: Butterworth, the flattest passband with no resonant bump at the corner.</summary>
            private static readonly double Q = 1.0 / Math.Sqrt(2.0);

            private readonly double _b0, _b1, _b2, _a1, _a2;
            private double _x1, _x2, _y1, _y2;

            private Biquad(double b0, double b1, double b2, double a1, double a2)
            {
                _b0 = b0; _b1 = b1; _b2 = b2; _a1 = a1; _a2 = a2;
            }

            public double Step(double x)
            {
                var y = _b0 * x + _b1 * _x1 + _b2 * _x2 - _a1 * _y1 - _a2 * _y2;
                _x2 = _x1; _x1 = x;
                _y2 = _y1; _y1 = y;
                return y;
            }

            public void Reset()
            {
                _x1 = 0.0; _x2 = 0.0; _y1 = 0.0; _y2 = 0.0;
            }

            public static Biquad HighPass(int sampleRate, double f0)
            {
                var w = 2.0 * Math.PI * f0 / sampleRate;
                var alpha = Math.Sin(w) / (2.0 * Q);
                var a0 = 1.0 + alpha;
                var c = Math.Cos(w);
                return new Biquad(
                    (1.0 + c) / 2.0 / a0, -(1.0 + c) / a0, (1.0 + c) / 2.0 / a0,
                    -2.0 * c / a0, (1.0 - alpha) / a0);
            }

            public static Biquad LowPass(int sampleRate, double f0)
            {
                var w = 2.0 * Math.PI * f0 / sampleRate;
                var alpha = Math.Sin(w) / (2.0 * Q);
                var a0 = 1.0 + alpha;
                var c = Math.Cos(w);
                return new Biquad(
                    (1.0 - c) / 2.0 / a0, (1.0 - c) / a0, (1.0 - c) / 2.0 / a0,
                    -2.0 * c / a0, (1.0 - alpha) / a0);
            }
        }
    }
}
