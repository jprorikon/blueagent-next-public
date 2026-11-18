using System.Collections.Generic;
using System.Linq;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>單人通話的斷句器（CallVad）：什麼時候算開口、什麼時候算一句說完、她在說時要多大聲才打得斷。</summary>
    public class CallVadTest
    {
        private const int Rate = CallVad.SampleRate;

        /// <summary>安靜房間（±40 的偽隨機底噪，約 -63 dBFS）。</summary>
        private static short[] Quiet(int ms, int seed = 1)
        {
            var random = new System.Random(seed);
            var samples = new short[Rate * ms / 1000];
            for (var i = 0; i < samples.Length; i++) samples[i] = (short)random.Next(-40, 41);
            return samples;
        }

        /// <summary>說話（440 Hz 正弦；振幅 3000 ≈ -24 dBFS）。</summary>
        private static short[] Voice(int ms, double amplitude = 3000)
        {
            var samples = new short[Rate * ms / 1000];
            for (var i = 0; i < samples.Length; i++)
                samples[i] = (short)(amplitude * System.Math.Sin(2 * System.Math.PI * 440 * i / Rate));
            return samples;
        }

        private static List<CallVad.Event> Feed(CallVad vad, params short[][] chunks)
        {
            var events = new List<CallVad.Event>();
            foreach (var chunk in chunks)
            {
                // 照真機那樣一小塊一小塊地餵（每塊 37 ms，故意不對齊 20 ms 一格）
                for (var at = 0; at < chunk.Length; at += 592)
                {
                    var size = System.Math.Min(592, chunk.Length - at);
                    var piece = new short[size];
                    System.Array.Copy(chunk, at, piece, 0, size);
                    events.AddRange(vad.Feed(piece, size));
                }
            }
            return events;
        }

        [Test]
        public void 安靜不出事件_噪音底跟到房間()
        {
            var vad = new CallVad();
            Assert.That(Feed(vad, Quiet(3000)), Is.Empty);
            Assert.That(vad.NoiseDb, Is.LessThan(-55f));
            Assert.That(vad.Speaking, Is.False);
        }

        [Test]
        public void 一句話_先開口再整句()
        {
            var vad = new CallVad();
            var events = Feed(vad, Quiet(1000), Voice(1000), Quiet(1200));
            Assert.That(events.Select(e => e.Kind),
                        Is.EqualTo(new[] { CallVad.Kind.Started, CallVad.Kind.Utterance }));
            var seconds = events[1].Samples.Length / (double)Rate;
            // 開口前一小段＋1 秒的話＋句尾留的那一點，不含後面那一秒多的空白
            Assert.That(seconds, Is.InRange(1.0, 1.0 + (vad.PreRollMs + 200 + 100) / 1000.0));
        }

        [Test]
        public void 句中換氣不切()
        {
            var vad = new CallVad();
            var events = Feed(vad, Quiet(800), Voice(600), Quiet(400), Voice(600), Quiet(1200));
            Assert.That(events.Count(e => e.Kind == CallVad.Kind.Utterance), Is.EqualTo(1));
        }

        [Test]
        public void 碰一下不算開口()
        {
            var vad = new CallVad();
            Assert.That(Feed(vad, Quiet(800), Voice(100), Quiet(1200)), Is.Empty);
        }

        [Test]
        public void 太短的丟掉()
        {
            var vad = new CallVad { MinSpeechMs = 400 };
            var events = Feed(vad, Quiet(800), Voice(240), Quiet(1200));
            Assert.That(events.Select(e => e.Kind),
                        Is.EqualTo(new[] { CallVad.Kind.Started, CallVad.Kind.Dropped }));
        }

        [Test]
        public void 她在說又沒有回音消除_小聲打不斷()
        {
            // 振幅 600 ≈ -38 dBFS：平常算開口，抬了回音餘量之後不算
            var normal = new CallVad();
            Assert.That(Feed(normal, Quiet(800), Voice(800, 600), Quiet(1000)).Select(e => e.Kind),
                        Has.Member(CallVad.Kind.Started));
            var echo = new CallVad { Echo = true };
            Assert.That(Feed(echo, Quiet(800), Voice(800, 600), Quiet(1000)), Is.Empty);
            Assert.That(Feed(echo, Voice(800, 6000)).Select(e => e.Kind),
                        Has.Member(CallVad.Kind.Started), "大聲說話照樣打得斷");
        }

        [Test]
        public void 說太久就先切一刀()
        {
            var vad = new CallVad { MaxSpeechMs = 3000 };
            var events = Feed(vad, Quiet(600), Voice(4000));
            var cut = events.Where(e => e.Kind == CallVad.Kind.Utterance).ToList();
            Assert.That(cut.Count, Is.EqualTo(1));
            Assert.That(cut[0].Samples.Length, Is.EqualTo(3000 * Rate / 1000));
        }

        [Test]
        public void 歸零之後重新聽()
        {
            var vad = new CallVad();
            Feed(vad, Quiet(800), Voice(500));
            Assert.That(vad.Speaking, Is.True);
            vad.Reset();
            Assert.That(vad.Speaking, Is.False);
            Assert.That(Feed(vad, Quiet(1500)), Is.Empty);
        }
    }
}
