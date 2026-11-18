using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text.Json;
using BlueAgent.Logic;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>
    /// **fixture 對拍**：`fixtures/settings_store.json` 是 Kotlin 那一側（`SettingsStore.kt`）由
    /// `SettingsStoreFixtureExportTest` 機器產生的真值——內容是**操作序列（ops）＋每一步的觀測（trace）**。
    ///
    /// 這裡把同一串 ops 餵進 C# 版逐條重放（後端＝<see cref="MapSettingsBackend"/>，含每個 case 的**種子值**
    /// ＝「使用者手機上已有的設定」），與 trace 逐字比對。⇒ 綠＝「兩邊在這批 case 上行為相同」，
    /// 而且是**有狀態的那一種相同**：寫進後端的鍵名與原生型別、有沒有多寫一個鍵、夾界、
    /// trim 的語意（含 Kotlin 的空白 ≠ .NET 的空白）、舊鍵名遷移、以及 `ensureSessionId` 的亂數消耗順序，
    /// 全都只反映在「每一步之後讀回來的樣子」上。
    ///
    /// 觀測字串的編碼與 Kotlin 那支**同一條**（見下面的 <see cref="Enc"/>）：字串 `s:`、整數 `i:`、
    /// 布林 `b:`、Float `f:`＋**原始位元**（十六進位）、Double `d:`＋**原始位元**（十進位）。
    /// 浮點記位元而不是記格式化後的字串，是為了釘住「值本身逐位元相同」（包含「Double 設定走的是
    /// 後端的 Float」那條精度損失）。
    ///
    /// fixture 是**產物**，不要手改（要加 case 就改 Kotlin 那支匯出測試再重跑）。
    /// </summary>
    [TestFixture]
    public class SettingsStoreFixtureTest
    {
        /// <summary>這條 fixture 的 case 數下限（匯出那支是不是忘了加 case）。</summary>
        private const int MinCases = 14;

        private JsonDocument _fixture;

        [SetUp]
        public void Load()
        {
            var path = Path.Combine(AppContext.BaseDirectory, "fixtures", "settings_store.json");
            Assert.That(File.Exists(path), Is.True,
                $"找不到 fixture：{path}（先在 Kotlin 側跑 SettingsStoreFixtureExportTest）");
            _fixture = JsonDocument.Parse(File.ReadAllText(path));
        }

        [TearDown]
        public void Unload() => _fixture?.Dispose();

        [Test]
        public void 逐條重放並比對軌跡()
        {
            var root = _fixture.RootElement;
            Assert.That(root.GetProperty("module").GetString(), Is.EqualTo("SettingsStore"), "fixture 的 module 不對");
            Assert.That(root.GetProperty("defaultSession").GetString(), Is.EqualTo(SettingsStore.DefaultSession),
                "出貨預設名與 Kotlin 不同");

            var cases = root.GetProperty("cases");
            var total = 0;
            foreach (var c in cases.EnumerateArray())
            {
                var name = c.GetProperty("name").GetString();
                var ops = c.GetProperty("ops");
                var trace = c.GetProperty("trace");

                Assert.That(trace.GetArrayLength(), Is.EqualTo(ops.GetArrayLength()),
                    $"[{name}] ops 與 trace 的步數不同（fixture 壞了）");

                var actual = Run(ops);
                var expected = new List<string>();
                foreach (var t in trace.EnumerateArray()) expected.Add(t.GetString());

                Assert.That(actual.Count, Is.EqualTo(expected.Count), $"[{name}] 步驟數不符");
                for (var i = 0; i < expected.Count; i++)
                {
                    Assert.That(actual[i], Is.EqualTo(expected[i]),
                        $"[{name}] 第 {i + 1} 步（op={OpName(ops[i])}）不符：" +
                        $"Kotlin={Brief(expected[i])} / C#={Brief(actual[i])}");
                }
                total += expected.Count;
            }

            var caseCount = cases.GetArrayLength();
            Assert.That(caseCount, Is.GreaterThanOrEqualTo(MinCases), "fixture 太少（匯出那支是不是忘了加 case）");
            TestContext.Out.WriteLine($"SettingsStore：{caseCount} 條 case／{total} 步與 Kotlin 版逐字相同");
        }

        /// <summary>fixture 本身要能重放（ops 的形狀固定；欄位名打錯會在這裡當場紅，而不是比對時看不懂）。</summary>
        [Test]
        public void fixture_的_ops_形狀合法()
        {
            var known = new HashSet<string>(StringComparer.Ordinal)
            {
                "open", "get", "set", "keys", "contains", "remove",
                "bk_get_string", "bk_get_int", "bk_get_bool", "bk_get_float", "ensure", "configured",
            };
            var cases = _fixture.RootElement.GetProperty("cases");
            foreach (var c in cases.EnumerateArray())
            {
                var name = c.GetProperty("name").GetString();
                var ops = c.GetProperty("ops");
                Assert.That(ops.GetArrayLength(), Is.GreaterThan(0), $"[{name}] 沒有 ops");
                var seenOpen = false;
                foreach (var op in ops.EnumerateArray())
                {
                    var kind = OpName(op);
                    Assert.That(known.Contains(kind), Is.True, $"[{name}] 不認得的 op：{kind}");
                    if (kind == "open") seenOpen = true;
                    else Assert.That(seenOpen, Is.True, $"[{name}] 第一個 op 不是 open（store 還不存在）");
                }
                Assert.That(seenOpen, Is.True, $"[{name}] 沒有 open");
            }
            TestContext.Out.WriteLine($"SettingsStore fixture 健檢：{cases.GetArrayLength()} 條 case 的 ops 形狀都合法");
        }

        // ── 重放器 ────────────────────────────────────────────────────────────────

        private static string OpName(JsonElement op) =>
            op.TryGetProperty("op", out var v) ? v.GetString() : "?";

        private static string Brief(string s) =>
            s == null ? "null" : (s.Length <= 200 ? s : s.Substring(0, 200) + "…");

        private static List<string> Run(JsonElement ops)
        {
            MapSettingsBackend backend = new MapSettingsBackend();
            SettingsStore store = null;
            var trace = new List<string>();
            foreach (var o in ops.EnumerateArray())
            {
                string r;
                try { r = Step(ref backend, ref store, o); }
                catch { r = "!THROW"; }
                trace.Add(r);
            }
            return trace;
        }

        private static string Step(ref MapSettingsBackend backend, ref SettingsStore store, JsonElement o)
        {
            var kind = o.GetProperty("op").GetString();
            switch (kind)
            {
                case "open":
                    backend = new MapSettingsBackend(Seed(o.GetProperty("seed")));
                    store = new SettingsStore(backend);
                    return "ok";
                case "get":
                    return GetField(store, o.GetProperty("field").GetString());
                case "set":
                    SetField(store, o.GetProperty("field").GetString(), o.GetProperty("value"));
                    return "ok";
                case "keys":
                    return JArray.OfStrings(backend.Keys()).ToString();
                case "contains":
                    return backend.Contains(o.GetProperty("key").GetString()) ? "true" : "false";
                case "remove":
                    backend.Remove(o.GetProperty("key").GetString());
                    return "ok";
                case "bk_get_string":
                    return Enc(backend.GetString(o.GetProperty("key").GetString(), o.GetProperty("def").GetString()));
                case "bk_get_int":
                    return Enc(backend.GetInt(o.GetProperty("key").GetString(), o.GetProperty("def").GetInt32()));
                case "bk_get_bool":
                    return Enc(backend.GetBoolean(o.GetProperty("key").GetString(), o.GetProperty("def").GetBoolean()));
                case "bk_get_float":
                    return Enc(backend.GetFloat(o.GetProperty("key").GetString(), (float)o.GetProperty("def").GetDouble()));
                case "ensure":
                    return store.EnsureSessionId(new ScriptedRandom(Ints(o.GetProperty("script"))));
                case "configured":
                    return GetField(store, "configured");
                default:
                    return "!UNKNOWN_OP:" + kind;
            }
        }

        /// <summary>種子值（＝「使用者手機上已有的設定」）：JSON 值的原生型別照 Kotlin 那一側的 `Map&lt;String, Any&gt;`。</summary>
        private static List<KeyValuePair<string, object>> Seed(JsonElement seed)
        {
            var outp = new List<KeyValuePair<string, object>>();
            foreach (var p in seed.EnumerateObject())
            {
                outp.Add(new KeyValuePair<string, object>(p.Name, Box(p.Value)));
            }
            return outp;
        }

        private static object Box(JsonElement v)
        {
            switch (v.ValueKind)
            {
                case JsonValueKind.String: return v.GetString();
                case JsonValueKind.True: return true;
                case JsonValueKind.False: return false;
                case JsonValueKind.Number:
                    // 整數值 ⇒ long、有小數 ⇒ double（Kotlin 那側的 `Map<String, Any>` 就是這樣）。
                    return v.TryGetInt64(out var l) ? (object)l : v.GetDouble();
                default: return null;
            }
        }

        private static List<int> Ints(JsonElement array)
        {
            var list = new List<int>();
            foreach (var item in array.EnumerateArray()) list.Add(item.GetInt32());
            return list;
        }

        private static string GetField(SettingsStore s, string field)
        {
            switch (field)
            {
                case "character": return Enc(s.Character);
                case "background": return Enc(s.Background);
                case "backgroundRandom": return Enc(s.BackgroundRandom);
                case "mode": return Enc(s.Mode);
                case "workMode": return Enc(s.WorkMode);
                case "inputLanguage": return Enc(s.InputLanguage);
                case "outputLanguage": return Enc(s.OutputLanguage);
                case "uiLocale": return Enc(s.UiLocale);
                case "outputVolume": return Enc(s.OutputVolume);
                case "speakReplies": return Enc(s.SpeakReplies);
                case "micMode": return Enc(s.MicMode);
                case "wakeWord": return Enc(s.WakeWord);
                case "kwsEnabled": return Enc(s.KwsEnabled);
                case "kwsThreshold": return Enc(s.KwsThreshold);
                case "bargeIn": return Enc(s.BargeIn);
                case "kwsDecideEvery": return Enc(s.KwsDecideEvery);
                case "kwsConsecutive": return Enc(s.KwsConsecutive);
                case "kwsCooldownMs": return Enc(s.KwsCooldownMs);
                case "speakerGate": return Enc(s.SpeakerGate);
                case "speakerThreshold": return Enc(s.SpeakerThreshold);
                case "spkSherpa": return Enc(s.SpkSherpa);
                case "spkSherpaThreshold": return Enc(s.SpkSherpaThreshold);
                case "spkSherpaProvider": return Enc(s.SpkSherpaProvider);
                case "spkSherpaThreads": return Enc(s.SpkSherpaThreads);
                case "gateMinEvalMs": return Enc(s.GateMinEvalMs);
                case "gateDelayMs": return Enc(s.GateDelayMs);
                case "gateRule": return Enc(s.GateRule);
                case "keepGateWindow": return Enc(s.KeepGateWindow);
                case "dumpWakeWindow": return Enc(s.DumpWakeWindow);
                case "conversationMs": return Enc(s.ConversationMs);
                case "outboxWindowMs": return Enc(s.OutboxWindowMs);
                case "silenceMs": return Enc(s.SilenceMs);
                case "realtimeSilenceMs": return Enc(s.RealtimeSilenceMs);
                case "minSpeechMs": return Enc(s.MinSpeechMs);
                case "vadMinRms": return Enc(s.VadMinRms);
                case "vadThresholdFactor": return Enc(s.VadThresholdFactor);
                case "prerollMs": return Enc(s.PrerollMs);
                case "vadRelaxFactor": return Enc(s.VadRelaxFactor);
                case "voiceFilter": return Enc(s.VoiceFilter);
                case "voiceHighPass": return Enc(s.VoiceHighPass);
                case "voiceLowPass": return Enc(s.VoiceLowPass);
                case "audioSource": return Enc(s.AudioSource);
                case "platformEffects": return Enc(s.PlatformEffects);
                case "maxUtteranceMs": return Enc(s.MaxUtteranceMs);
                case "ackEnabled": return Enc(s.AckEnabled);
                case "bridgeUrl": return Enc(s.BridgeUrl);
                case "bridgeToken": return Enc(s.BridgeToken);
                case "bridgeSession": return Enc(s.BridgeSession);
                case "configured": return Enc(s.Configured);
                default: return "!UNKNOWN_FIELD:" + field;
            }
        }

        private static void SetField(SettingsStore s, string field, JsonElement v)
        {
            switch (field)
            {
                case "character": s.Character = v.GetString(); break;
                case "background": s.Background = v.GetString(); break;
                case "backgroundRandom": s.BackgroundRandom = v.GetBoolean(); break;
                case "mode": s.Mode = v.GetString(); break;
                case "workMode": s.WorkMode = v.GetString(); break;
                case "inputLanguage": s.InputLanguage = v.GetString(); break;
                case "outputLanguage": s.OutputLanguage = v.GetString(); break;
                case "uiLocale": s.UiLocale = v.GetString(); break;
                case "outputVolume": s.OutputVolume = v.GetInt32(); break;
                case "speakReplies": s.SpeakReplies = v.GetBoolean(); break;
                case "micMode": s.MicMode = v.GetString(); break;
                case "wakeWord": s.WakeWord = v.GetString(); break;
                case "kwsEnabled": s.KwsEnabled = v.GetBoolean(); break;
                case "kwsThreshold": s.KwsThreshold = (float)v.GetDouble(); break;
                case "bargeIn": s.BargeIn = v.GetBoolean(); break;
                case "kwsDecideEvery": s.KwsDecideEvery = v.GetInt32(); break;
                case "kwsConsecutive": s.KwsConsecutive = v.GetInt32(); break;
                case "kwsCooldownMs": s.KwsCooldownMs = v.GetInt32(); break;
                case "speakerGate": s.SpeakerGate = v.GetBoolean(); break;
                case "speakerThreshold": s.SpeakerThreshold = (float)v.GetDouble(); break;
                case "spkSherpa": s.SpkSherpa = v.GetString(); break;
                case "spkSherpaThreshold": s.SpkSherpaThreshold = (float)v.GetDouble(); break;
                case "spkSherpaProvider": s.SpkSherpaProvider = v.GetString(); break;
                case "spkSherpaThreads": s.SpkSherpaThreads = v.GetInt32(); break;
                case "gateMinEvalMs": s.GateMinEvalMs = v.GetInt32(); break;
                case "gateDelayMs": s.GateDelayMs = v.GetInt32(); break;
                case "gateRule": s.GateRule = v.GetString(); break;
                case "keepGateWindow": s.KeepGateWindow = v.GetBoolean(); break;
                case "dumpWakeWindow": s.DumpWakeWindow = v.GetBoolean(); break;
                case "conversationMs": s.ConversationMs = v.GetInt32(); break;
                case "outboxWindowMs": s.OutboxWindowMs = v.GetInt32(); break;
                case "silenceMs": s.SilenceMs = v.GetInt32(); break;
                case "realtimeSilenceMs": s.RealtimeSilenceMs = v.GetInt32(); break;
                case "minSpeechMs": s.MinSpeechMs = v.GetInt32(); break;
                case "vadMinRms": s.VadMinRms = v.GetDouble(); break;
                case "vadThresholdFactor": s.VadThresholdFactor = v.GetDouble(); break;
                case "prerollMs": s.PrerollMs = v.GetInt32(); break;
                case "vadRelaxFactor": s.VadRelaxFactor = v.GetDouble(); break;
                case "voiceFilter": s.VoiceFilter = v.GetBoolean(); break;
                case "voiceHighPass": s.VoiceHighPass = v.GetDouble(); break;
                case "voiceLowPass": s.VoiceLowPass = v.GetDouble(); break;
                case "audioSource": s.AudioSource = v.GetString(); break;
                case "platformEffects": s.PlatformEffects = v.GetBoolean(); break;
                case "maxUtteranceMs": s.MaxUtteranceMs = v.GetInt32(); break;
                case "ackEnabled": s.AckEnabled = v.GetBoolean(); break;
                case "bridgeUrl": s.BridgeUrl = v.GetString(); break;
                case "bridgeToken": s.BridgeToken = v.GetString(); break;
                case "bridgeSession": s.BridgeSession = v.GetString(); break;
                default: throw new ArgumentException("unknown field: " + field);
            }
        }

        // ── 觀測編碼（與 Kotlin 那支同一條）──────────────────────────────────────

        private static string Enc(object v)
        {
            if (v == null) return "null";
            if (v is string s) return "s:" + s;
            if (v is bool b) return b ? "b:true" : "b:false";
            if (v is int i) return "i:" + i.ToString(CultureInfo.InvariantCulture);
            if (v is long l) return "l:" + l.ToString(CultureInfo.InvariantCulture);
            // Float／Double 走**原始位元**（見檔頭）。
            if (v is float f) return "f:" + unchecked((uint)BitConverter.SingleToInt32Bits(f)).ToString("x", CultureInfo.InvariantCulture);
            if (v is double d) return "d:" + BitConverter.DoubleToInt64Bits(d).ToString(CultureInfo.InvariantCulture);
            return "?" + v;
        }

        /// <summary>
        /// 腳本化亂數：照 fixture 的 `script` 依呼叫序 pop（與 Kotlin 那支 ScriptedRandom 同一條規則；
        /// 只有 2 次冪的 bound——十六進位 16 與 variant 4）。
        /// </summary>
        private sealed class ScriptedRandom : IRandomSource
        {
            private readonly List<int> _script;
            private int _index;

            public ScriptedRandom(IEnumerable<int> script) => _script = new List<int>(script);

            public int NextInt(int bound)
            {
                if (bound <= 0) throw new ArgumentOutOfRangeException(nameof(bound), bound, "bound 必須 > 0");
                var mask = bound - 1;
                if ((bound & mask) != 0)
                    throw new NotSupportedException($"腳本化亂數只實作 2 次冪的 bound；bound={bound}");
                if (_index >= _script.Count)
                    throw new InvalidOperationException($"腳本用完（已消耗 {_index} 個，腳本長度 {_script.Count}）");
                var n = _script[_index++];
                var raw = unchecked((uint)n * 0x11111111u);
                return (int)(raw & (uint)mask);
            }
        }
    }
}
