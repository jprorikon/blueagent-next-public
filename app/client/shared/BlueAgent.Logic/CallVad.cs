using System;
using System.Collections.Generic;

namespace BlueAgent.Logic
{
    /// <summary>
    /// 單人通話的斷句器（2026-10-29 owner：「單人通話模式，實時和 agent 對話」「能打斷」）：
    /// 一直聽的 16 kHz 單聲道 PCM16 ⇒ 「開口了」（<see cref="CallVad.Kind.Started"/>，打斷用）
    /// ＋「一句說完了」（<see cref="CallVad.Kind.Utterance"/>，整句送 `/api/audio`）。
    ///
    /// 為什麼在端末切、而不是串流給伺服器切：伺服器只有「一段音檔 → 一輪」的入口（SSE 契約，
    /// 不開 WebSocket），而且一句一送，STT 吃到的就是完整的一句。判斷用能量（RMS dBFS）：
    /// <list type="bullet">
    /// <item>門檻＝max(絕對下限, 噪音底＋餘量)；噪音底只在「沒在說話」時追（降得快、升得慢）；</item>
    /// <item>連續超過門檻 <see cref="StartMs"/> 才算開口（咳一聲、碰一下桌子不算）；</item>
    /// <item>開口前的 <see cref="PreRollMs"/> 一起送（不然每句都少第一個音）；</item>
    /// <item>低於門檻 <see cref="EndMs"/> 才算說完（句中換氣不切）；太短的丟掉（<see cref="Kind.Dropped"/>）；</item>
    /// <item><see cref="Echo"/>：她正在說、而且平台沒掛上回音消除 ⇒ 門檻再抬 <see cref="EchoMarginDb"/>，
    ///   免得喇叭漏進麥克風的她自己的聲音把她打斷（要打斷就得比她大聲）。</item>
    /// </list>
    /// 純計算、不碰 Unity：`dotnet test` 釘著（<c>CallVadTest</c>）。
    /// </summary>
    public sealed class CallVad
    {
        public const int SampleRate = 16000;
        /// <summary>一格 20 ms。</summary>
        public const int FrameSamples = SampleRate / 50;
        private const int FrameMs = 20;

        public enum Kind
        {
            /// <summary>開口了（打斷她用；這時還沒有音檔）。</summary>
            Started,
            /// <summary>一句說完：<see cref="Event.Samples"/> 是整句（含開口前那一小段）。</summary>
            Utterance,
            /// <summary>開了口但太短（咳嗽、「嗯」）：不送。</summary>
            Dropped,
        }

        public struct Event
        {
            public Kind Kind;
            public short[] Samples;
        }

        /// <summary>算「有聲音」的絕對下限（dBFS）：再安靜的房間也不會比這更敏感。</summary>
        public float StartDb = -42f;
        /// <summary>比噪音底高多少才算說話。</summary>
        public float NoiseMarginDb = 12f;
        /// <summary>她在說、又沒有回音消除時，門檻再抬多少。</summary>
        public float EchoMarginDb = 10f;
        /// <summary>說話中的門檻比開口時低一點（遲滯），句尾變小聲不會被切掉。</summary>
        public float HoldDropDb = 4f;
        public int StartMs = 160;
        public int EndMs = 700;
        public int MinSpeechMs = 300;
        public int MaxSpeechMs = 15000;
        public int PreRollMs = 300;

        /// <summary>她正在說話、而且平台的回音消除沒掛上。</summary>
        public bool Echo { get; set; }

        /// <summary>現在在一句話裡面。</summary>
        public bool Speaking { get; private set; }

        /// <summary>最近一格的電平（dBFS；給畫面的音量條）。</summary>
        public float LevelDb { get; private set; } = -90f;

        /// <summary>估出來的噪音底（dBFS）。</summary>
        public float NoiseDb { get; private set; } = -60f;

        private readonly short[] _frame = new short[FrameSamples];
        private int _frameFill;
        /// <summary>沒在說話時的最近幾格（開口前那一段＋判定開口那幾格）。</summary>
        private readonly Queue<short[]> _recent = new Queue<short[]>();
        private readonly List<short[]> _utterance = new List<short[]>();
        private int _voicedRun;
        private int _silenceRun;
        private int _voicedTotal;

        /// <summary>現在的開口門檻（dBFS）。</summary>
        public float Threshold
        {
            get { return Math.Max(StartDb, NoiseDb + NoiseMarginDb) + (Echo ? EchoMarginDb : 0f); }
        }

        /// <summary>餵一批樣本（任意長度），回這一批裡發生的事（照順序）。</summary>
        public List<Event> Feed(short[] samples, int count)
        {
            var events = new List<Event>();
            if (samples == null) return events;
            count = Math.Min(count, samples.Length);
            for (var i = 0; i < count; i++)
            {
                _frame[_frameFill++] = samples[i];
                if (_frameFill < FrameSamples) continue;
                _frameFill = 0;
                Step((short[])_frame.Clone(), events);
            }
            return events;
        }

        /// <summary>全部歸零（掛斷、靜音、她說完一輪之後重新聽都可以用）。噪音底留著。</summary>
        public void Reset()
        {
            Speaking = false;
            _frameFill = 0;
            _recent.Clear();
            _utterance.Clear();
            _voicedRun = 0;
            _silenceRun = 0;
            _voicedTotal = 0;
        }

        private void Step(short[] frame, List<Event> events)
        {
            var db = Db(frame);
            LevelDb = db;
            if (!Speaking)
            {
                TrackNoise(db);
                _recent.Enqueue(frame);
                var keep = (PreRollMs + StartMs) / FrameMs + 1;
                while (_recent.Count > keep) _recent.Dequeue();
                _voicedRun = db >= Threshold ? _voicedRun + FrameMs : 0;
                if (_voicedRun < StartMs) return;
                Speaking = true;
                _utterance.Clear();
                _utterance.AddRange(_recent);
                _recent.Clear();
                _voicedTotal = _voicedRun;
                _silenceRun = 0;
                events.Add(new Event { Kind = Kind.Started });
                return;
            }

            _utterance.Add(frame);
            if (db >= Threshold - HoldDropDb)
            {
                _silenceRun = 0;
                _voicedTotal += FrameMs;
            }
            else
            {
                _silenceRun += FrameMs;
            }
            var length = _utterance.Count * FrameMs;
            if (_silenceRun >= EndMs)
            {
                // 句尾的靜音只留一小段（STT 不需要那 0.7 秒的空白）
                var trailing = Math.Max(0, (_silenceRun - 200) / FrameMs);
                Finish(events, _voicedTotal >= MinSpeechMs, trailing);
            }
            else if (length >= MaxSpeechMs)
            {
                Finish(events, true, 0);
            }
        }

        private void Finish(List<Event> events, bool keep, int dropTail)
        {
            var frames = Math.Max(0, _utterance.Count - dropTail);
            if (keep)
            {
                var samples = new short[frames * FrameSamples];
                for (var i = 0; i < frames; i++)
                    Array.Copy(_utterance[i], 0, samples, i * FrameSamples, FrameSamples);
                events.Add(new Event { Kind = Kind.Utterance, Samples = samples });
            }
            else
            {
                events.Add(new Event { Kind = Kind.Dropped });
            }
            Speaking = false;
            _utterance.Clear();
            _voicedRun = 0;
            _silenceRun = 0;
            _voicedTotal = 0;
        }

        /// <summary>噪音底：比現在小就快速跟下去、比現在大就慢慢爬（說話不會把它拉上去）。</summary>
        private void TrackNoise(float db)
        {
            var rate = db < NoiseDb ? 0.2f : 0.02f;
            NoiseDb = Math.Max(-80f, Math.Min(-30f, NoiseDb + (db - NoiseDb) * rate));
        }

        private static float Db(short[] frame)
        {
            double sum = 0;
            for (var i = 0; i < frame.Length; i++) sum += (double)frame[i] * frame[i];
            var rms = Math.Sqrt(sum / Math.Max(1, frame.Length));
            if (rms < 1) return -90f;
            return (float)(20.0 * Math.Log10(rms / 32768.0));
        }
    }
}
