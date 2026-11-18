using System;
using System.Collections.Generic;
using System.IO;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using BlueAgent.Logic;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>
    /// **SSE 切分器**的測試。真值有兩條：
    ///
    /// ① **離線（預設跑）**：`fixtures/bridge_protocol.json` 的 41 條 `parse_event` case 就是
    ///    「`event` ＋ `data`」那一對（Kotlin 機器產生的真值）。測試把**那一對**用橋自己的組幀法
    ///    （<see cref="SseStream.FrameBytes"/>＝`bridge_voice.sse()` 那一行）包成位元組 ⇒ 餵進切分器
    ///    ⇒ 要求**原樣吐回同一對**（`event` 名字 trim 後、`data` 原樣）。
    ///    「切分器的責任」就只到這裡：位元組 → 與真值相同的 `(event, data)`；
    ///    至於 `(event, data) → 幀`，那 41 條已經由 `BridgeProtocolFixtureTest` 逐欄釘住。
    ///
    /// ② **在線（`BA_SSE_LIVE=1` 才跑）**：拿**真的橋**（本機 :8900）的 `/chat` 串流餵同一支切分器。
    ///    為什麼要這一條：2026-10-12 的教訓——「自造 fixture 要用線上的真實形狀」（嘴型腳本那次
    ///    兩邊一致地錯、測試一路綠）。離線那條用的是真值 payload ＋ 橋的組幀法，但**切塊方式**
    ///    （TCP 怎麼切）只有在線那條驗得到；實測那一串 615 KB 被切成 157 塊、塊界與行界無關。
    /// </summary>
    [TestFixture]
    public class SseStreamTest
    {
        private JsonDocument _fixture;

        [SetUp]
        public void Load()
        {
            var path = Path.Combine(AppContext.BaseDirectory, "fixtures", "bridge_protocol.json");
            Assert.That(File.Exists(path), Is.True, $"找不到 fixture：{path}");
            _fixture = JsonDocument.Parse(File.ReadAllText(path));
        }

        [TearDown]
        public void Unload() => _fixture?.Dispose();

        /// <summary>真值的 41 對 `(event, data)`（＝橋會在網路上送的那些幀）。</summary>
        private List<(string Event, string Data)> TruthFramePairs()
        {
            var list = new List<(string, string)>();
            foreach (var c in _fixture.RootElement.GetProperty("cases").EnumerateArray())
            {
                if (c.GetProperty("op").GetString() != "parse_event") continue;
                var a = c.GetProperty("args");
                list.Add((a.GetProperty("event").GetString(), a.GetProperty("data").GetString()));
            }
            return list;
        }

        private static List<SseStream.Wire> Feed(byte[] bytes, Func<long, int> nextChunk)
        {
            var stream = new SseStream();
            var all = new List<SseStream.Wire>();
            var pos = 0;
            long step = 0;
            while (pos < bytes.Length)
            {
                var n = Math.Min(nextChunk(step), bytes.Length - pos);
                all.AddRange(stream.Push(bytes, pos, n));
                pos += n;
                step++;
            }
            all.AddRange(stream.Flush());
            return all;
        }

        [Test]
        public void 真值的每一對都原樣吐回來_整段與逐位元組都相同()
        {
            var pairs = TruthFramePairs();
            Assert.That(pairs.Count, Is.GreaterThanOrEqualTo(41), "真值的 parse_event 條數不對");

            foreach (var (ev, data) in pairs)
            {
                var bytes = SseStream.FrameBytes(ev, data);
                foreach (var chunk in new Func<long, int>[] { _ => bytes.Length, _ => 1, _ => 3 })
                {
                    var wires = Feed(bytes, chunk);
                    Assert.That(wires.Count, Is.EqualTo(1), $"[{ev}] 幀數不對（{wires.Count}）");
                    Assert.That(wires[0].Event, Is.EqualTo(KotlinText.Trim(ev)), $"[{ev}] event 名字不符");
                    // `data:` 的值**原樣**（不 trim）——裁切是 ParseEvent 的事。
                    // ⚠️ 橋的組幀是 `data: {json}` ⇒ 切出來的值以**一個空白**開頭（Kotlin 的 substring(5)
                    //    就是這樣；標準 SSE 會把那個空白吃掉，這裡刻意不吃）。
                    Assert.That(wires[0].Data, Is.EqualTo(" " + data), $"[{ev}] data 內容不符");
                }
            }
            TestContext.Out.WriteLine($"SseStream：{pairs.Count} 對真值 × 3 種切法，全部原樣");
        }

        [Test]
        public void 整條串流任意切塊_幀序與內容都一樣()
        {
            var pairs = TruthFramePairs();
            var all = new MemoryStream();
            foreach (var (ev, data) in pairs)
            {
                var b = SseStream.FrameBytes(ev, data);
                all.Write(b, 0, b.Length);
            }
            var bytes = all.ToArray();

            var whole = Feed(bytes, _ => bytes.Length);
            Assert.That(whole.Count, Is.EqualTo(pairs.Count), "整段餵進去的幀數不對");

            var random = new Random(20261012);
            foreach (var chunk in new Func<long, int>[]
                     {
                         _ => 1,
                         _ => 2,
                         _ => 7,
                         _ => 64,
                         _ => (int)random.Next(1, 97),   // 亂數塊界（固定種子）
                     })
            {
                var wires = Feed(bytes, chunk);
                Assert.That(wires.Count, Is.EqualTo(whole.Count), "切塊之後幀數變了");
                for (var i = 0; i < wires.Count; i++)
                {
                    Assert.That(wires[i].Event, Is.EqualTo(whole[i].Event), $"第 {i} 幀 event 不符");
                    Assert.That(wires[i].Data, Is.EqualTo(whole[i].Data), $"第 {i} 幀 data 不符");
                }
            }
            TestContext.Out.WriteLine($"SseStream：{pairs.Count} 幀／{bytes.Length} 位元組 × 5 種切法（含亂數塊界）完全一致");
        }

        [Test]
        public void 多位元組字元被切在塊中間也解得回來()
        {
            const string text = "{\"text\":\"確認しました。聞こえています。\"}";
            var bytes = SseStream.FrameBytes("delta", text);
            // 逐位元組＝一定切在日文字元的 3 個位元組中間
            var wires = Feed(bytes, _ => 1);
            Assert.That(wires.Count, Is.EqualTo(1));
            Assert.That(wires[0].Event, Is.EqualTo("delta"));
            Assert.That(wires[0].Data, Is.EqualTo(" " + text));
            // 而且真的解得出文字（不是一堆 U+FFFD）
            Assert.That(wires[0].Data.Contains("確認しました"), Is.True);
            Assert.That(wires[0].Data.Contains('\uFFFD'), Is.False);
        }

        [Test]
        public void 切在_data_前綴中間也認得出來()
        {
            var bytes = SseStream.FrameBytes("started", "{\"run_id\":\"r1\"}");
            var wires = Feed(bytes, step => step == 1 ? 3 : bytes.Length);   // 第 2 塊正好切在 `dat|a:` 中間
            Assert.That(wires.Count, Is.EqualTo(1));
            Assert.That(wires[0].Event, Is.EqualTo("started"));
            Assert.That(wires[0].Data, Is.EqualTo(" {\"run_id\":\"r1\"}"));
        }

        [Test]
        public void event_名字跨空行保留_註解與其他行不影響()
        {
            const string raw = ": keep-alive\nid: 7\nevent: read\n\ndata: {\"src\":\"type\"}\n\n";
            var wires = new SseStream().PushText(raw);
            Assert.That(wires.Count, Is.EqualTo(1), "只有一個 data: 行 ⇒ 只有一幀");
            Assert.That(wires[0].Event, Is.EqualTo("read"));
            Assert.That(wires[0].Data, Is.EqualTo(" {\"src\":\"type\"}"));
        }

        [Test]
        public void event_名字帶空白要照_Kotlin_的_trim_裁()
        {
            // Kotlin：`substring(6).trim()` —— NBSP（U+00A0）在 Kotlin 的 trim 裡**要裁**（見 KotlinText）。
            var wires = new SseStream().PushText("event:  read\u00A0\ndata: {}\n\n");
            Assert.That(wires.Count, Is.EqualTo(1));
            Assert.That(wires[0].Event, Is.EqualTo("read"));
        }

        [Test]
        public void 多個_data_行各自成一幀_名字沿用()
        {
            var wires = new SseStream().PushText("event: delta\ndata: A\ndata: B\n\n");
            Assert.That(wires.Count, Is.EqualTo(2), "Kotlin 是一行一解（不是把多行黏起來）");
            Assert.That(wires[0].Data, Is.EqualTo(" A"));
            Assert.That(wires[1].Data, Is.EqualTo(" B"));
            Assert.That(wires[1].Event, Is.EqualTo("delta"));
        }

        [Test]
        public void 沒有_event_行的_data_名字是空的_幀本身不算數()
        {
            var wires = new SseStream().PushText("data: {\"src\":\"type\"}\n\n");
            Assert.That(wires.Count, Is.EqualTo(1));
            Assert.That(wires[0].Event, Is.EqualTo(""));
            // 而且交給協定層＝這一幀等於沒來（Kotlin 的 `parseEvent("", …)` 回 null）。
            Assert.That(BridgeProtocol.ParseEvent(wires[0].Event, wires[0].Data), Is.Null);
        }

        [Test]
        public void CRLF_與_單獨的_LF_兩種行尾都吃()
        {
            var wires = new SseStream().PushText("event: done\r\ndata: {\"text\":\"好\"}\r\n\r\n");
            Assert.That(wires.Count, Is.EqualTo(1));
            Assert.That(wires[0].Event, Is.EqualTo("done"));
            Assert.That(wires[0].Data, Is.EqualTo(" {\"text\":\"好\"}"));
        }

        [Test]
        public void 最後一行沒有換行_Flush_之後仍算一幀_且只吐一次()
        {
            var stream = new SseStream();
            var wires = stream.PushText("event: done\ndata: {\"text\":\"尾\"}");
            Assert.That(wires.Count, Is.EqualTo(0), "還沒換行 ⇒ 還不成一幀");
            Assert.That(stream.PartialLength, Is.GreaterThan(0));

            var tail = stream.Flush();
            Assert.That(tail.Count, Is.EqualTo(1));
            Assert.That(tail[0].Event, Is.EqualTo("done"));
            Assert.That(tail[0].Data, Is.EqualTo(" {\"text\":\"尾\"}"));
            Assert.That(stream.Flush().Count, Is.EqualTo(0), "Flush 可以重複呼叫，第二次要回空的");
        }

        [Test]
        public void 沒有_data_行的_event_不吐幀()
        {
            Assert.That(new SseStream().PushText("event: started\n\n").Count, Is.EqualTo(0));
        }

        /// <summary>
        /// **在線**：真的橋（`BA_BRIDGE_URL`，預設 `http://127.0.0.1:8900`）的 `/chat` 串流。
        /// 只有 `BA_SSE_LIVE=1` 才跑（要 `BA_BRIDGE_TOKEN`）——NAS 上跑得到，CI／別人的機器上會被略過。
        /// 判據：① 幀序 `read → started → delta → done`（順序是契約）；
        /// ② `delta` 的內容非空；③ 串流**真的被切成很多塊**（塊界與行界無關）。
        /// </summary>
        [Test]
        public void 在線_真的橋的串流()
        {
            if (Environment.GetEnvironmentVariable("BA_SSE_LIVE") != "1")
                Assert.Ignore("略過在線測試（要 BA_SSE_LIVE=1 ＋ BA_BRIDGE_TOKEN）");
            var token = Environment.GetEnvironmentVariable("BA_BRIDGE_TOKEN") ?? "";
            Assert.That(token, Is.Not.Empty, "BA_SSE_LIVE=1 但沒有 BA_BRIDGE_TOKEN");
            var baseUrl = Environment.GetEnvironmentVariable("BA_BRIDGE_URL") ?? "http://127.0.0.1:8900";

            var payload = BridgeProtocol.ChatPayload("疎通確認です。一言だけ返して。",
                                                     "hermes-unity-spike", "aster", "chat", tts: false);
            using var http = new HttpClient { Timeout = TimeSpan.FromMinutes(3) };
            using var req = new HttpRequestMessage(HttpMethod.Post, baseUrl.TrimEnd('/') + "/chat")
            {
                Content = new StringContent(payload, Encoding.UTF8, "application/json"),
            };
            foreach (var h in BridgeProtocol.Headers(token, "unity-spike"))
                req.Headers.TryAddWithoutValidation(h.Key, h.Value);

            using var resp = http.Send(req, HttpCompletionOption.ResponseHeadersRead);
            Assert.That((int)resp.StatusCode, Is.EqualTo(200), "橋沒有回 200");
            using var body = resp.Content.ReadAsStream();

            var stream = new SseStream();
            var frames = new List<BridgeProtocol.Frame>();
            var chunkSizes = new List<int>();
            var buf = new byte[4096];
            int n;
            while ((n = body.Read(buf, 0, buf.Length)) > 0)
            {
                chunkSizes.Add(n);
                foreach (var w in stream.Push(buf, 0, n))
                {
                    var f = BridgeProtocol.ParseEvent(w.Event, w.Data);
                    if (f != null) frames.Add(f);
                }
            }
            foreach (var w in stream.Flush())
            {
                var f = BridgeProtocol.ParseEvent(w.Event, w.Data);
                if (f != null) frames.Add(f);
            }

            var kinds = frames.ConvertAll(f => f.Kind);
            TestContext.Out.WriteLine($"在線：{chunkSizes.Count} 塊／{frames.Count} 幀 ⇒ {string.Join(" → ", kinds)}");
            foreach (var f in frames)
                TestContext.Out.WriteLine($"  {f.Kind}：{(f.Text.Length > 60 ? f.Text.Substring(0, 60) + "…" : f.Text)}");

            Assert.That(chunkSizes.Count, Is.GreaterThan(1), "只收到一塊 ⇒ 這條測試沒有驗到切塊");
            Assert.That(kinds, Does.Contain("read").And.Contain("started").And.Contain("delta").And.Contain("done"));
            Assert.That(kinds.IndexOf("read"), Is.LessThan(kinds.IndexOf("started")), "read 要在 started 之前");
            Assert.That(kinds.IndexOf("started"), Is.LessThan(kinds.IndexOf("delta")), "started 要在 delta 之前");
            Assert.That(kinds.IndexOf("delta"), Is.LessThan(kinds.IndexOf("done")), "delta 要在 done 之前");
            var delta = frames.Find(f => f.Kind == "delta");
            Assert.That(delta.Text.Length, Is.GreaterThan(0), "delta 是空的");
        }
    }
}
