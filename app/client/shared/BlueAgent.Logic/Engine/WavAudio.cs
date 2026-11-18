using System;
using System.Collections.Generic;

namespace BlueAgent.Logic.Engine
{
    /// <summary>
    /// **WAV 的解碼與電平** —— 橋送來的 `speech.audio` 是一段 base64 的 WAV（見 `BridgeProtocol.Speech`），
    /// 而各平台要「播它、並從它算嘴型的量測音量」。
    ///
    /// 為什麼放在共用邏輯層：**解碼與電平的算術是純的**（沒有平台 API），放這裡就能在 NAS 上用單測釘住；
    /// 各平台只剩下「把樣本丟給自己的播放器」那一格（Unity 用 `AudioClip`、Android 用 `MediaPlayer`）。
    ///
    /// 支援的格式：PCM 8／16／24／32 位、IEEE float 32／64 位、G.711 μ-law 8 位，單聲道或多聲道；
    /// **不支援**（直接丟例外，不要靜靜地播成雜音）：ADPCM 之類的壓縮格式、WAVE_FORMAT_EXTENSIBLE 以外的
    /// 奇怪 tag、缺 `fmt `／`data` 區塊。
    /// </summary>
    public static class WavAudio
    {
        /// <summary>解出來的波形：取樣率、聲道數、以及**交錯的**單精度樣本（範圍 −1..1）。</summary>
        public sealed class Clip
        {
            public readonly int SampleRate;
            public readonly int Channels;
            public readonly float[] Samples;

            public Clip(int sampleRate, int channels, float[] samples)
            {
                SampleRate = sampleRate;
                Channels = channels;
                Samples = samples;
            }

            /// <summary>每個聲道的格數（＝整段長度／聲道數）。</summary>
            public int FrameCount => Channels > 0 ? Samples.Length / Channels : 0;

            /// <summary>長度（秒）。</summary>
            public double Seconds => SampleRate > 0 ? (double)FrameCount / SampleRate : 0.0;
        }

        /// <summary>解一段 WAV。壞掉的輸入丟 <see cref="FormatException"/>（不要回半個 Clip 讓人以為成功了）。</summary>
        public static Clip Parse(byte[] bytes)
        {
            if (bytes == null || bytes.Length < 44) throw new FormatException("WAV 太短");
            if (Ascii(bytes, 0, 4) != "RIFF" || Ascii(bytes, 8, 4) != "WAVE")
                throw new FormatException("不是 RIFF/WAVE");

            var offset = 12;
            var format = 0;
            var channels = 0;
            var sampleRate = 0;
            var bits = 0;
            var dataOffset = -1;
            var dataLength = 0;

            while (offset + 8 <= bytes.Length)
            {
                var id = Ascii(bytes, offset, 4);
                var size = BitConverter.ToInt32(bytes, offset + 4);
                var body = offset + 8;
                if (size < 0 || body + size > bytes.Length)
                {
                    // 最後一塊的大小偶爾會多報（有的寫檔器把 pad 位元算錯）⇒ 只在「data」上放寬。
                    if (id == "data") size = bytes.Length - body;
                    else break;
                }

                switch (id)
                {
                    case "fmt ":
                        format = BitConverter.ToInt16(bytes, body);
                        channels = BitConverter.ToInt16(bytes, body + 2);
                        sampleRate = BitConverter.ToInt32(bytes, body + 4);
                        bits = BitConverter.ToInt16(bytes, body + 14);
                        if (format == unchecked((short)0xFFFE) && size >= 26)
                        {
                            // WAVE_FORMAT_EXTENSIBLE：真正的格式在最後那個 GUID 的前兩個位元組。
                            format = BitConverter.ToInt16(bytes, body + 24);
                        }
                        break;
                    case "data":
                        dataOffset = body;
                        dataLength = size;
                        break;
                }

                offset = body + size + (size % 2);       // 區塊以偶數對齊
                if (dataOffset >= 0 && format != 0) break;
            }

            if (format == 0 || channels <= 0 || sampleRate <= 0)
                throw new FormatException("缺 fmt 區塊（或內容不完整）");
            if (dataOffset < 0) throw new FormatException("缺 data 區塊");

            var bytesPerSample = bits / 8;
            if (bytesPerSample <= 0) throw new FormatException($"位元深度不認得：{bits}");
            var count = dataLength / bytesPerSample;
            var samples = new float[count];

            switch (format)
            {
                case 1:      // PCM
                    for (var i = 0; i < count; i++)
                    {
                        var at = dataOffset + i * bytesPerSample;
                        samples[i] = bits switch
                        {
                            8 => (bytes[at] - 128) / 128f,
                            16 => BitConverter.ToInt16(bytes, at) / 32768f,
                            24 => ToInt24(bytes, at) / 8388608f,
                            32 => BitConverter.ToInt32(bytes, at) / 2147483648f,
                            _ => throw new FormatException($"PCM 位元深度不認得：{bits}"),
                        };
                    }
                    break;
                case 7:      // G.711 μ-law（App 的 mulaw-v1；8-bit，純算術解碼，iOS／Android 一致）
                    if (bits != 8) throw new FormatException($"μ-law 位元深度不認得：{bits}");
                    for (var i = 0; i < count; i++) samples[i] = DecodeMuLaw(bytes[dataOffset + i]);
                    break;
                case 3:      // IEEE float
                    if (bits == 32)
                    {
                        for (var i = 0; i < count; i++) samples[i] = BitConverter.ToSingle(bytes, dataOffset + i * 4);
                    }
                    else if (bits == 64)
                    {
                        for (var i = 0; i < count; i++) samples[i] = (float)BitConverter.ToDouble(bytes, dataOffset + i * 8);
                    }
                    else
                    {
                        throw new FormatException($"float 位元深度不認得：{bits}");
                    }
                    break;
                default:
                    throw new FormatException($"壓縮格式不支援（audioFormat={format}）——別靜靜地播成雜音");
            }

            return new Clip(sampleRate, channels, samples);
        }

        /// <summary>
        /// 一段樣本的**電平與峰值**（`AudioLevelAnalyzer` 要的那兩顆）。
        ///
        /// 定義（照 Android 那一側的用法）：`level` ＝ RMS、`peak` ＝ |樣本| 的最大值，都在 0..1。
        /// 空的一段回 (0, 0)。
        /// </summary>
        public static (double Level, double Peak) Levels(IReadOnlyList<float> samples, int from = 0, int count = -1)
        {
            if (samples == null || samples.Count == 0) return (0.0, 0.0);
            if (count < 0) count = samples.Count - from;
            if (from < 0 || from >= samples.Count || count <= 0) return (0.0, 0.0);
            var end = Math.Min(samples.Count, from + count);

            double sum = 0.0;
            double peak = 0.0;
            var n = 0;
            for (var i = from; i < end; i++)
            {
                var v = samples[i];
                var a = Math.Abs((double)v);
                if (a > peak) peak = a;
                sum += (double)v * v;
                n++;
            }
            if (n == 0) return (0.0, 0.0);
            return (Math.Sqrt(sum / n), peak);
        }

        private static float DecodeMuLaw(byte encoded)
        {
            // ITU-T G.711：先取反，再還原符號／指數／尾數與量化偏置。
            var value = ~encoded & 0xFF;
            var sample = (((value & 0x0F) << 3) + 0x84) << ((value >> 4) & 0x07);
            sample -= 0x84;
            return ((value & 0x80) != 0 ? sample : -sample) / 32768f;
        }

        private static int ToInt24(byte[] bytes, int at)
        {
            var v = bytes[at] | (bytes[at + 1] << 8) | (bytes[at + 2] << 16);
            return (v & 0x800000) != 0 ? v | unchecked((int)0xFF000000) : v;
        }

        private static string Ascii(byte[] bytes, int at, int length)
        {
            if (at + length > bytes.Length) return "";
            var chars = new char[length];
            for (var i = 0; i < length; i++) chars[i] = (char)bytes[at + i];
            return new string(chars);
        }

        /// <summary>做一段測試用的 WAV（16 位 PCM；每個聲道同一個值）。</summary>
        public static byte[] Build(int sampleRate, int channels, int frames, Func<int, float> sampleAt) =>
            Build(sampleRate, channels, frames, (frame, channel) => sampleAt(frame));

        /// <summary>做一段測試用的 WAV（16 位 PCM；每個聲道可以給不同的值 ⇒ 做得出「一左一右」）。</summary>
        public static byte[] Build(int sampleRate, int channels, int frames, Func<int, int, float> sampleAt)
        {
            var dataLength = frames * channels * 2;
            var bytes = new byte[44 + dataLength];
            WriteAscii(bytes, 0, "RIFF");
            BitConverter.GetBytes(36 + dataLength).CopyTo(bytes, 4);
            WriteAscii(bytes, 8, "WAVE");
            WriteAscii(bytes, 12, "fmt ");
            BitConverter.GetBytes(16).CopyTo(bytes, 16);
            BitConverter.GetBytes((short)1).CopyTo(bytes, 20);
            BitConverter.GetBytes((short)channels).CopyTo(bytes, 22);
            BitConverter.GetBytes(sampleRate).CopyTo(bytes, 24);
            BitConverter.GetBytes(sampleRate * channels * 2).CopyTo(bytes, 28);
            BitConverter.GetBytes((short)(channels * 2)).CopyTo(bytes, 32);
            BitConverter.GetBytes((short)16).CopyTo(bytes, 34);
            WriteAscii(bytes, 36, "data");
            BitConverter.GetBytes(dataLength).CopyTo(bytes, 40);
            for (var i = 0; i < frames; i++)
            {
                for (var ch = 0; ch < channels; ch++)
                {
                    var value = Math.Max(-1f, Math.Min(1f, sampleAt(i, ch)));
                    BitConverter.GetBytes((short)(value * 32767f)).CopyTo(bytes, 44 + (i * channels + ch) * 2);
                }
            }
            return bytes;
        }

        private static void WriteAscii(byte[] bytes, int at, string text)
        {
            for (var i = 0; i < text.Length; i++) bytes[at + i] = (byte)text[i];
        }
    }
}
