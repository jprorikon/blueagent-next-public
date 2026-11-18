using System;
using System.Collections.Generic;
using BlueAgent.Logic.Engine;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>
    /// `WavAudio`（橋送來的 `speech.audio` 解碼 ＋ 量測電平）的規格測試。
    ///
    /// 為什麼這一支要釘住：**解錯格式不會「壞掉」，只會播成雜音**（位元深度算錯 ⇒ 全是噪音），
    /// 而且**電平算錯會讓嘴不動或一直張開**——兩種都不會有人當場發現。
    /// </summary>
    [TestFixture]
    public class WavAudioTest
    {
        [Test]
        public void 十六位單聲道_解出來的長度與樣本都對()
        {
            var frames = 4800;                                  // 48kHz／0.1 秒
            var bytes = WavAudio.Build(48000, 1, frames, i => (float)Math.Sin(2 * Math.PI * 440 * i / 48000.0));
            var clip = WavAudio.Parse(bytes);

            Assert.That(clip.SampleRate, Is.EqualTo(48000));
            Assert.That(clip.Channels, Is.EqualTo(1));
            Assert.That(clip.Samples.Length, Is.EqualTo(frames));
            Assert.That(clip.FrameCount, Is.EqualTo(frames));
            Assert.That(clip.Seconds, Is.EqualTo(0.1).Within(1e-9));
            Assert.That(clip.Samples[0], Is.EqualTo(0f).Within(1e-4));
            // 16 位量化 ⇒ 逐點誤差不超過半個刻度
            for (var i = 0; i < frames; i += 137)
            {
                var want = Math.Sin(2 * Math.PI * 440 * i / 48000.0);
                Assert.That(clip.Samples[i], Is.EqualTo(want).Within(2.0 / 32768), $"第 {i} 點");
            }
        }

        [Test]
        public void 立體聲是交錯的_格數要除以聲道數()
        {
            var bytes = WavAudio.Build(44100, 2, 100, (frame, channel) => channel == 0 ? 0.5f : -0.5f);
            var clip = WavAudio.Parse(bytes);

            Assert.That(clip.Channels, Is.EqualTo(2));
            Assert.That(clip.Samples.Length, Is.EqualTo(200), "樣本數＝格數 × 聲道數");
            Assert.That(clip.FrameCount, Is.EqualTo(100));
            Assert.That(clip.Samples[0], Is.EqualTo(0.5f).Within(1e-4));
            Assert.That(clip.Samples[1], Is.EqualTo(-0.5f).Within(1e-4));
        }

        [Test]
        public void 八位與二十四位與浮點都要認得()
        {
            // 8 位：128 ＝ 0、255 ≈ +1、0 ＝ −1（無號，偏 128）
            var eight = new byte[44 + 3];
            WriteHeader(eight, format: 1, channels: 1, sampleRate: 8000, bits: 8, dataLength: 3);
            eight[44] = 128;
            eight[45] = 255;
            eight[46] = 0;
            var clip8 = WavAudio.Parse(eight);
            Assert.That(clip8.Samples[0], Is.EqualTo(0f).Within(1e-6));
            Assert.That(clip8.Samples[1], Is.EqualTo(127f / 128).Within(1e-6));
            Assert.That(clip8.Samples[2], Is.EqualTo(-1f).Within(1e-6));

            // 24 位：0x7FFFFF ＝ 最大正、0x800000 ＝ 最小負
            var twentyFour = new byte[44 + 6];
            WriteHeader(twentyFour, 1, 1, 16000, 24, 6);
            twentyFour[44] = 0xFF; twentyFour[45] = 0xFF; twentyFour[46] = 0x7F;
            twentyFour[47] = 0x00; twentyFour[48] = 0x00; twentyFour[49] = 0x80;
            var clip24 = WavAudio.Parse(twentyFour);
            Assert.That(clip24.Samples[0], Is.EqualTo(1f).Within(1e-5));
            Assert.That(clip24.Samples[1], Is.EqualTo(-1f).Within(1e-5));

            // float32
            var floatBytes = new byte[44 + 8];
            WriteHeader(floatBytes, 3, 1, 48000, 32, 8);
            BitConverter.GetBytes(0.25f).CopyTo(floatBytes, 44);
            BitConverter.GetBytes(-0.75f).CopyTo(floatBytes, 48);
            var clipF = WavAudio.Parse(floatBytes);
            Assert.That(clipF.Samples[0], Is.EqualTo(0.25f));
            Assert.That(clipF.Samples[1], Is.EqualTo(-0.75f));
        }

        [Test]
        public void G711_mu_law_用純算術還原_不依賴平台編解碼器()
        {
            // audioop.ulaw2lin(bytes([FF, 7F, 80, 00]), 2)＝[0, 0, -32124, 32124]
            var bytes = new byte[44 + 4];
            WriteHeader(bytes, format: 7, channels: 1, sampleRate: 16000, bits: 8, dataLength: 4);
            bytes[44] = 0xFF; bytes[45] = 0x7F; bytes[46] = 0x80; bytes[47] = 0x00;
            var clip = WavAudio.Parse(bytes);
            Assert.That(clip.SampleRate, Is.EqualTo(16000));
            Assert.That(clip.Samples[0], Is.EqualTo(0f).Within(1e-6));
            Assert.That(clip.Samples[1], Is.EqualTo(0f).Within(1e-6));
            Assert.That(clip.Samples[2], Is.EqualTo(-32124f / 32768).Within(1e-6));
            Assert.That(clip.Samples[3], Is.EqualTo(32124f / 32768).Within(1e-6));
        }

        [Test]
        public void 電平與峰值_定義要對_而且要能只算一段()
        {
            var samples = new List<float> { 0f, 0.5f, -0.5f, 0f };
            var (level, peak) = WavAudio.Levels(samples);
            Assert.That(peak, Is.EqualTo(0.5).Within(1e-9), "峰值＝|樣本| 的最大值");
            Assert.That(level, Is.EqualTo(Math.Sqrt(0.5 / 4)).Within(1e-9), "電平＝RMS");

            var (winLevel, winPeak) = WavAudio.Levels(samples, from: 1, count: 2);
            Assert.That(winPeak, Is.EqualTo(0.5).Within(1e-9));
            Assert.That(winLevel, Is.EqualTo(0.5).Within(1e-9), "只算中間兩點 ⇒ RMS 剛好 0.5");

            var (emptyLevel, emptyPeak) = WavAudio.Levels(new List<float>());
            Assert.That(emptyLevel, Is.EqualTo(0.0));
            Assert.That(emptyPeak, Is.EqualTo(0.0));

            var (oofLevel, oofPeak) = WavAudio.Levels(samples, from: 99, count: 4);
            Assert.That((oofLevel, oofPeak), Is.EqualTo((0.0, 0.0)), "越界要回 0，不要炸");
        }

        [Test]
        public void 壞掉的輸入要丟例外_不要回半個clip()
        {
            Assert.Throws<FormatException>(() => WavAudio.Parse(new byte[10]), "太短");
            Assert.Throws<FormatException>(() => WavAudio.Parse(new byte[100]), "不是 RIFF");
            Assert.Throws<FormatException>(() => WavAudio.Parse(null));

            // 有 fmt 沒有 data
            var noData = new byte[36];
            WriteAscii(noData, 0, "RIFF");
            WriteAscii(noData, 8, "WAVE");
            WriteAscii(noData, 12, "fmt ");
            BitConverter.GetBytes(16).CopyTo(noData, 16);
            BitConverter.GetBytes((short)1).CopyTo(noData, 20);
            BitConverter.GetBytes((short)1).CopyTo(noData, 22);
            BitConverter.GetBytes(48000).CopyTo(noData, 24);
            BitConverter.GetBytes((short)16).CopyTo(noData, 34);
            Assert.Throws<FormatException>(() => WavAudio.Parse(noData), "缺 data");

            // 壓縮格式（ADPCM＝2）要**拒絕**，不要靜靜地播成雜音
            var compressed = new byte[44 + 4];
            WriteHeader(compressed, 2, 1, 8000, 4, 4);
            Assert.Throws<FormatException>(() => WavAudio.Parse(compressed), "壓縮格式不支援");
        }

        [Test]
        public void WAVE_FORMAT_EXTENSIBLE_要走GUID認回PCM()
        {
            var bytes = WavAudio.Build(48000, 1, 10, i => 0.1f);
            // 把 fmt 改成 EXTENSIBLE（0xFFFE）＋在偏移 24 塞上 PCM 的格式碼
            var fmt = 12;
            BitConverter.GetBytes(16).CopyTo(bytes, fmt + 4);              // size 還是 16（寬鬆接受）
            BitConverter.GetBytes(unchecked((short)0xFFFE)).CopyTo(bytes, fmt + 8);
            Assert.Throws<FormatException>(() => WavAudio.Parse(bytes),
                "size 不夠放 GUID ⇒ 當成壞掉（這一條是防呆，不是支援）");

            // 佈局：RIFF(12) ＋ fmt(8＋40＝20..59) ＋ data(8＋4＝60..71)
            var wide = new byte[72];
            WriteAscii(wide, 0, "RIFF");
            WriteAscii(wide, 8, "WAVE");
            WriteAscii(wide, 12, "fmt ");
            BitConverter.GetBytes(40).CopyTo(wide, 16);                    // EXTENSIBLE 的 fmt 是 40 bytes
            BitConverter.GetBytes(unchecked((short)0xFFFE)).CopyTo(wide, 20);
            BitConverter.GetBytes((short)1).CopyTo(wide, 22);
            BitConverter.GetBytes(48000).CopyTo(wide, 24);
            BitConverter.GetBytes(96000).CopyTo(wide, 28);
            BitConverter.GetBytes((short)2).CopyTo(wide, 32);
            BitConverter.GetBytes((short)16).CopyTo(wide, 34);
            BitConverter.GetBytes((short)22).CopyTo(wide, 36);
            BitConverter.GetBytes((short)16).CopyTo(wide, 38);
            BitConverter.GetBytes((short)1).CopyTo(wide, 44);              // ← 真正的格式碼（GUID 的前兩個位元組 ＝ PCM）
            WriteAscii(wide, 60, "data");
            BitConverter.GetBytes(4).CopyTo(wide, 64);
            BitConverter.GetBytes((short)16384).CopyTo(wide, 68);
            BitConverter.GetBytes((short)-16384).CopyTo(wide, 70);

            var clip = WavAudio.Parse(wide);
            Assert.That(clip.Channels, Is.EqualTo(1));
            Assert.That(clip.Samples.Length, Is.EqualTo(2));
            Assert.That(clip.Samples[0], Is.EqualTo(0.5f).Within(1e-4));
        }

        private static void WriteHeader(byte[] bytes, short format, short channels, int sampleRate, short bits, int dataLength)
        {
            WriteAscii(bytes, 0, "RIFF");
            BitConverter.GetBytes(36 + dataLength).CopyTo(bytes, 4);
            WriteAscii(bytes, 8, "WAVE");
            WriteAscii(bytes, 12, "fmt ");
            BitConverter.GetBytes(16).CopyTo(bytes, 16);
            BitConverter.GetBytes(format).CopyTo(bytes, 20);
            BitConverter.GetBytes(channels).CopyTo(bytes, 22);
            BitConverter.GetBytes(sampleRate).CopyTo(bytes, 24);
            BitConverter.GetBytes(sampleRate * channels * (bits / 8)).CopyTo(bytes, 28);
            BitConverter.GetBytes((short)(channels * (bits / 8))).CopyTo(bytes, 32);
            BitConverter.GetBytes(bits).CopyTo(bytes, 34);
            WriteAscii(bytes, 36, "data");
            BitConverter.GetBytes(dataLength).CopyTo(bytes, 40);
        }

        private static void WriteAscii(byte[] bytes, int at, string text)
        {
            for (var i = 0; i < text.Length; i++) bytes[at + i] = (byte)text[i];
        }
    }
}
