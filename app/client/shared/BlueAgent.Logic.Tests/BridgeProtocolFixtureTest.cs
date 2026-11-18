using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using System.Text.Json;
using BlueAgent.Logic;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>
    /// **fixture 對拍**：`fixtures/bridge_protocol.json` 是 Kotlin 那一側（`BridgeProtocol.kt`）由
    /// `BridgeProtocolFixtureExportTest` 機器產生的真值——形狀是「**一 case 一次呼叫**：`args` → `out`」。
    ///
    /// 這裡把同一個 op 用 C# 版跑一遍、用**同一套 render 規則**印出來逐字比對 ⇒
    /// 綠＝「兩邊的 payload 組法、SSE 事件→幀、四個解碼器的語意，以及文字／數字的格式逐字元相同」。
    ///
    /// 兩個關鍵細節：
    ///   * **`null` 與空字串是兩件不同的事**（`null`＝這一幀沒來／解析失敗；`""`＝合法的空結果，
    ///     例如 `HistoryTurns` 回空清單）——fixture 分開記，這裡也就分開比。
    ///   * 壞掉的輸入在 Kotlin 那側是「有沒有丟」而不是「丟哪一種」（例外類別名天生不同）⇒ 這裡
    ///     一律記 `!THROW`。
    ///
    /// fixture 是**產物**，不要手改。
    /// </summary>
    [TestFixture]
    public class BridgeProtocolFixtureTest
    {
        private JsonDocument _fixture;

        [SetUp]
        public void Load()
        {
            var path = Path.Combine(AppContext.BaseDirectory, "fixtures", "bridge_protocol.json");
            Assert.That(File.Exists(path), Is.True,
                $"找不到 fixture：{path}（先在 Kotlin 側跑 BridgeProtocolFixtureExportTest 匯出）");
            _fixture = JsonDocument.Parse(File.ReadAllText(path));
        }

        [TearDown]
        public void Unload() => _fixture?.Dispose();

        [Test]
        public void 逐條重放並比對()
        {
            var cases = _fixture.RootElement.GetProperty("cases");
            var total = 0;
            foreach (var c in cases.EnumerateArray())
            {
                var name = c.GetProperty("name").GetString();
                var op = c.GetProperty("op").GetString();
                var args = c.GetProperty("args");
                var outEl = c.GetProperty("out");
                var expected = outEl.ValueKind == JsonValueKind.Null ? null : outEl.GetString();

                // 一 case 就是一次呼叫 ⇒ 「第 1 步」；丟例外記 `!THROW`（與 Kotlin 那支的 run() 相同）。
                string actual;
                try { actual = Dispatch(op, args); }
                catch { actual = "!THROW"; }

                Assert.That(actual, Is.EqualTo(expected),
                    $"[{name}] 第 1 步（op={op}）不符：\n  Kotlin={Show(expected)}\n  C#    ={Show(actual)}");
                total++;
            }
            TestContext.Out.WriteLine($"BridgeProtocol：{total} 条 case 与 Kotlin 版相同");
        }

        private static string Show(string s) => s == null ? "null" : "\"" + s + "\"";

        // ── 重放（與 Kotlin 那支 run() 逐條對應）───────────────────────────────

        private static string Dispatch(string op, JsonElement a)
        {
            switch (op)
            {
                case "chat_payload":
                    return BridgeProtocol.ChatPayload(
                        S(a, "text"), S(a, "session"), S(a, "soul"), S(a, "mode"), B(a, "tts"), S(a, "image"));

                case "loc_payload":
                    return BridgeProtocol.LocPayload(D(a, "lat"), D(a, "lon"), F(a, "acc"));

                case "ends_settings":
                    return BridgeProtocol.EndsSettingsPayload(
                        S(a, "device"), S(a, "session"), S(a, "character"), S(a, "mode"),
                        B(a, "tts"), S(a, "end"));

                case "ends_channel":
                    return BridgeProtocol.EndsChannelPayload(
                        S(a, "device"), S(a, "channel_id"), S(a, "channel_name"), S(a, "character"));

                // headers 是 List ⇒ 用「插入序 ＋ k=v」壓成字串（順序是契約的一部分）。
                case "headers":
                {
                    var hs = BridgeProtocol.Headers(S(a, "token"), S(a, "device"), B(a, "sse"));
                    var sb = new StringBuilder();
                    for (var i = 0; i < hs.Count; i++)
                    {
                        if (i > 0) sb.Append('\n');
                        sb.Append(hs[i].Key).Append('=').Append(hs[i].Value);
                    }
                    return sb.ToString();
                }

                case "game_payload":
                    return BridgeProtocol.GamePayload(
                        S(a, "game_op"), S(a, "id"), I(a, "x"), I(a, "y"),
                        S(a, "difficulty"), S(a, "previous"), S(a, "character"), B(a, "voice"));

                case "parse_event":
                {
                    var f = BridgeProtocol.ParseEvent(S(a, "event"), S(a, "data"));
                    return f == null ? null : RenderFrame(f);
                }

                case "decode_speech":
                    return RenderSpeech(BridgeProtocol.DecodeSpeech(new JObject(S(a, "data"))));
                case "decode_narration":
                    return RenderNarration(BridgeProtocol.DecodeNarration(new JObject(S(a, "data"))));
                case "decode_media":
                    return RenderMedia(BridgeProtocol.DecodeMedia(new JObject(S(a, "data"))));
                case "decode_reminder":
                    return RenderReminder(BridgeProtocol.DecodeReminder(new JObject(S(a, "data"))));

                case "parse_game_reply":
                    return RenderGameReply(BridgeProtocol.ParseGameReply(S(a, "body")));

                case "history_turns":
                    return RenderTurns(BridgeProtocol.HistoryTurns(S(a, "body")));

                default:
                    return "!UNKNOWN_OP:" + op;
            }
        }

        // ── 渲染（與 Kotlin 那支 render* 逐字對應；格式見 fixture 的 exporter 檔頭）──

        private static string RenderMotion(List<BridgeProtocol.Motion> m)
        {
            var sb = new StringBuilder();
            for (var i = 0; i < m.Count; i++)
            {
                if (i > 0) sb.Append(',');
                sb.Append(m[i].AtMs.ToString(CultureInfo.InvariantCulture)).Append(':').Append(m[i].Shape);
            }
            return sb.ToString();
        }

        private static string RenderSpeech(BridgeProtocol.Speech s)
        {
            if (s == null) return null;
            var sb = new StringBuilder();
            sb.Append("S(").Append(s.Index.ToString(CultureInfo.InvariantCulture))
              .Append(',').Append(s.Total.ToString(CultureInfo.InvariantCulture))
              .Append(',').Append(s.Text)
              .Append(',').Append(JNum(s.Seconds))
              .Append(',').Append(s.AudioBase64)
              .Append(',').Append(RenderMotion(s.Motion))
              .Append(',').Append(s.Last ? "1" : "0").Append(')');
            return sb.ToString();
        }

        private static string RenderNarration(BridgeProtocol.Narration n)
        {
            if (n == null) return null;
            return "N(" + n.Character + "," + n.Clip + "," + n.Text + "," + JNum(n.Seconds) + ","
                   + n.AudioBase64 + "," + RenderMotion(n.Motion) + ")";
        }

        private static string RenderMedia(BridgeProtocol.Media m)
        {
            if (m == null) return null;
            return "M(" + m.Kind + "," + m.Url + "," + m.Name + ","
                   + m.Bytes.ToString(CultureInfo.InvariantCulture) + "," + (m.Replay ? "1" : "0") + ")";
        }

        private static string RenderReminder(BridgeProtocol.Reminder r)
        {
            if (r == null) return null;
            return "R(" + r.Id + "," + r.Text + "," + r.Say + "," + JNum(r.At) + ","
                   + r.Clock + "," + r.LateMin.ToString(CultureInfo.InvariantCulture) + ")";
        }

        private static string RenderFrame(BridgeProtocol.Frame f)
        {
            var parts = new string[]
            {
                f.Kind, f.Src, f.RunId, f.Session, f.Voice ? "1" : "0", f.Text, f.Tool, f.State,
                f.Preview, f.Message, f.OwnerEnd, f.Usage,
                f.Speech == null ? "-" : RenderSpeech(f.Speech),
                f.Narration == null ? "-" : RenderNarration(f.Narration),
                f.Media == null ? "-" : RenderMedia(f.Media),
                f.Reminder == null ? "-" : RenderReminder(f.Reminder),
            };
            return string.Join(";", parts);
        }

        private static string RenderGameReply(BridgeProtocol.GameReply g) =>
            (g.Ok ? "1" : "0") + ";" + g.Error + ";" + g.Frame + ";"
            + (g.Voice == null ? "-" : RenderSpeech(g.Voice));

        private static string RenderTurns(List<BridgeProtocol.Turn> ts)
        {
            var sb = new StringBuilder();
            for (var i = 0; i < ts.Count; i++)
            {
                if (i > 0) sb.Append('\n');
                sb.Append(ts[i].Role).Append('=').Append(ts[i].Content);
            }
            return sb.ToString();
        }

        // ── args 讀取 ──────────────────────────────────────────────────────────

        private static string S(JsonElement a, string k) =>
            a.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : "";

        private static bool B(JsonElement a, string k) =>
            a.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.True;

        private static int I(JsonElement a, string k) =>
            a.TryGetProperty(k, out var v) ? v.GetInt32() : 0;

        private static double D(JsonElement a, string k) =>
            a.TryGetProperty(k, out var v) ? v.GetDouble() : 0.0;

        /// <summary>`acc` 是 Float——Kotlin 那側 `Float.toDouble()` 的加寬在這裡是 `(float)` 反向收回。</summary>
        private static float F(JsonElement a, string k) =>
            a.TryGetProperty(k, out var v) ? (float)v.GetDouble() : 0f;

        // ── Kotlin／Java 的 Double.toString() ───────────────────────────────────

        /// <summary>
        /// **`JavaDouble.ToString` 的在地副本**（`Json.cs` 那一支是 `internal`，測試組件取不到；
        /// 而秒數／`at` 這些欄位在 fixture 裡就是用它印出來的）。
        ///
        /// 為什麼不能只用 `ToString("R")`：`1791283560.0` 在 Java 是 `1.79128356E9`（`10^7` 以上走
        /// 科學記號），`5.0` 要帶 `.0`、`1.5E-7` 的指數**不補零**——這些都與 .NET 的預設格式不同。
        /// </summary>
        private static string JNum(double d)
        {
            if (double.IsNaN(d)) return "NaN";
            if (double.IsPositiveInfinity(d)) return "Infinity";
            if (double.IsNegativeInfinity(d)) return "-Infinity";
            if (d == 0.0) return double.IsNegative(d) ? "-0.0" : "0.0";

            var sign = d < 0 ? "-" : "";
            var a = Math.Abs(d);
            var r = a.ToString("R", CultureInfo.InvariantCulture);

            var mantissa = r;
            var exp10 = 0;
            var eIdx = r.IndexOfAny(new[] { 'E', 'e' });
            if (eIdx >= 0)
            {
                mantissa = r.Substring(0, eIdx);
                exp10 = int.Parse(r.Substring(eIdx + 1), CultureInfo.InvariantCulture);
            }
            var dot = mantissa.IndexOf('.');
            if (dot >= 0)
            {
                exp10 -= mantissa.Length - dot - 1;
                mantissa = mantissa.Substring(0, dot) + mantissa.Substring(dot + 1);
            }
            var lead = 0;
            while (lead < mantissa.Length - 1 && mantissa[lead] == '0') lead++;
            mantissa = mantissa.Substring(lead);
            while (mantissa.Length > 1 && mantissa[mantissa.Length - 1] == '0')
            {
                mantissa = mantissa.Substring(0, mantissa.Length - 1);
                exp10++;
            }

            var digits = mantissa;
            var decExp = digits.Length + exp10;

            if (decExp > -3 && decExp <= 7)
            {
                var sb = new StringBuilder();
                if (decExp <= 0)
                {
                    sb.Append("0.");
                    for (var k = 0; k < -decExp; k++) sb.Append('0');
                    sb.Append(digits);
                }
                else if (digits.Length <= decExp)
                {
                    sb.Append(digits);
                    for (var k = 0; k < decExp - digits.Length; k++) sb.Append('0');
                    sb.Append(".0");
                }
                else
                {
                    sb.Append(digits.Substring(0, decExp)).Append('.').Append(digits.Substring(decExp));
                }
                return sign + sb.ToString();
            }

            var exp = decExp - 1;
            var frac = digits.Length > 1 ? digits.Substring(1) : "0";
            return sign + digits[0] + "." + frac + "E" + exp.ToString(CultureInfo.InvariantCulture);
        }
    }
}
