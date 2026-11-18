using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using BlueAgent.Logic.Engine;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>
    /// **fixture 對拍（M4：待機小動作 `IdleActionScheduler`）**：`fixtures/engine_idle_action.json`
    /// 由 Kotlin 側 `EngineIdleActionFixtureExportTest` 錄製。
    ///
    /// 逐幀比 `out`（疊在 FACS 上的那一層）**以及 `state`**：內部的「最近 N 個不重複」「方向不連續同向」
    /// 只有 state 看得到。九條 case 各自踩一條分支（視窗關掉／視窗＝1／能力只列三項／能力全 false／
    /// 只有眨眼／`spontaneity`＝0／開關＋抑制＋interrupt＋reset／VAD 與性格在動）。
    /// </summary>
    [TestFixture]
    public class EngineIdleActionFixtureTest
    {
        private JsonDocument _fixture;
        private double _tolerance;

        [SetUp]
        public void Load()
        {
            var path = Path.Combine(AppContext.BaseDirectory, "fixtures", "engine_idle_action.json");
            Assert.That(File.Exists(path), Is.True, $"找不到 fixture：{path}（先在 Kotlin 側跑 `gradle :engine:test`）");
            _fixture = JsonDocument.Parse(File.ReadAllText(path));
            _tolerance = _fixture.RootElement.GetProperty("tolerance").GetDouble();
        }

        [TearDown]
        public void Unload() => _fixture?.Dispose();

        [Test]
        public void 待機小動作逐幀重放()
        {
            var cases = _fixture.RootElement.GetProperty("cases");
            var frames = 0;
            var starts = 0;

            foreach (var c in cases.EnumerateArray())
            {
                var name = c.GetProperty("name").GetString();
                var config = c.GetProperty("config");
                var scheduler = new IdleActionScheduler(
                    seed: c.GetProperty("seed").GetDouble(),
                    spontaneity: ReadOptDouble(config, "spontaneity"),
                    gain: ReadOptDouble(config, "gain"),
                    minIntervalSeconds: ReadOptDouble(config, "minIntervalSeconds"),
                    maxIntervalSeconds: ReadOptDouble(config, "maxIntervalSeconds"),
                    recentWindowSize: ReadOptDouble(config, "recentWindowSize"));
                var profile = ModelProfile.FromJson(c.GetProperty("profileText").GetString());
                var personality = ReadPersonality(c.GetProperty("personality"));

                var previousActive = (string)null;
                foreach (var f in c.GetProperty("frames").EnumerateArray())
                {
                    var t = f.GetProperty("t").GetDouble();
                    var ctx = $"{name}@t={t}";

                    // ⚠️ 順序照 Kotlin：**先**執行腳本動作，**再** update（同一幀）
                    if (f.TryGetProperty("op", out var op)) ApplyOp(scheduler, op);

                    var vad = f.GetProperty("vad");
                    var context = new IdleActionContext(
                        enabled: f.GetProperty("enabled").GetBoolean(),
                        focusLevel: f.GetProperty("focus").GetDouble(),
                        vad: new VadRuntimeState(
                            current: new VadVector(
                                vad.GetProperty("valence").GetDouble(),
                                vad.GetProperty("arousal").GetDouble(),
                                vad.GetProperty("dominance").GetDouble()),
                            target: new VadVector(0, 0, 0),
                            dominantEmotion: "",
                            intensity: 0.0),
                        profile: profile,
                        personality: personality,
                        suppressed: f.GetProperty("suppressed").GetBoolean());

                    var actual = scheduler.Update(t, context);

                    var expectedOut = ReadMap(f.GetProperty("out"));
                    Assert.That(actual.Count, Is.EqualTo(expectedOut.Count),
                        $"{ctx}：out 鍵數不符（C#={actual.Count} / Kotlin={expectedOut.Count}）");
                    foreach (var kv in expectedOut)
                    {
                        Assert.That(actual.ContainsKey(kv.Key), Is.True, $"{ctx}：C# 少了鍵 {kv.Key}");
                        if (Math.Abs(actual[kv.Key] - kv.Value) > _tolerance)
                            Assert.Fail($"{ctx}.out.{kv.Key}：Kotlin={kv.Value} / C#={actual[kv.Key]}");
                    }

                    var state = scheduler.GetState();
                    var exp = f.GetProperty("state");
                    var activeAction = exp.GetProperty("activeAction");
                    if (activeAction.ValueKind == JsonValueKind.Null)
                        Assert.That(state.ActiveAction, Is.Null, ctx + " state.activeAction");
                    else if (state.ActiveAction != activeAction.GetString())
                        Assert.Fail($"{ctx} state.activeAction：Kotlin={activeAction.GetString()} / C#={state.ActiveAction}");

                    var expDirection = exp.GetProperty("direction").GetInt32();
                    if (state.Direction != expDirection)
                        Assert.Fail($"{ctx} state.direction：Kotlin={expDirection} / C#={state.Direction}");
                    AssertOptClose(state.StartedAt, exp.GetProperty("startedAt"), ctx + " state.startedAt");
                    AssertClose(state.Duration, exp.GetProperty("duration").GetDouble(), ctx + " state.duration");
                    AssertOptClose(state.NextActionAt, exp.GetProperty("nextActionAt"), ctx + " state.nextActionAt");
                    AssertList(state.RecentActions, exp.GetProperty("recentActions"), ctx + " state.recentActions");
                    AssertIntList(state.RecentDirections, exp.GetProperty("recentDirections"), ctx + " state.recentDirections");
                    var expSuppressed = exp.GetProperty("suppressed").GetBoolean();
                    if (state.Suppressed != expSuppressed)
                        Assert.Fail($"{ctx} state.suppressed：Kotlin={expSuppressed} / C#={state.Suppressed}");

                    if (state.ActiveAction != null && previousActive == null) starts++;
                    previousActive = state.ActiveAction;
                    frames++;
                }
            }

            TestContext.Out.WriteLine(
                $"待機小動作：{cases.GetArrayLength()} 條 case／{frames} 幀／動作起點 {starts} 次全數相同（含 state）");
            // ⚠️ 覆蓋率自檢：沒有動作起點就等於什麼都沒驗（能力篩選會讓某幾條 case 恰好 0 次）
            Assert.That(starts, Is.GreaterThanOrEqualTo(20), "動作起點太少 ⇒ fixture 沒走到幾條分支");
        }

        private static void ApplyOp(IdleActionScheduler scheduler, JsonElement op)
        {
            var kind = op.GetProperty("kind").GetString();
            var t = op.GetProperty("t").GetDouble();
            switch (kind)
            {
                case "interrupt":
                    scheduler.Interrupt(t);
                    break;
                case "reset":
                    scheduler.Reset(t);
                    break;
                default:
                    Assert.Fail("不認得的 idle action op kind=" + kind);
                    break;
            }
        }

        private static double? ReadOptDouble(JsonElement el, string key) =>
            el.TryGetProperty(key, out var v) ? v.GetDouble() : (double?)null;

        private static CharacterPersonality ReadPersonality(JsonElement el)
        {
            if (el.ValueKind == JsonValueKind.Null) return null;
            return new CharacterPersonality(
                expressiveness: el.GetProperty("expressiveness").GetDouble(),
                softness: el.GetProperty("softness").GetDouble(),
                shyness: el.GetProperty("shyness").GetDouble(),
                gazeStability: el.GetProperty("gazeStability").GetDouble());
        }

        private static Dictionary<string, float> ReadMap(JsonElement el)
        {
            var outp = new Dictionary<string, float>();
            foreach (var p in el.EnumerateObject()) outp[p.Name] = p.Value.GetSingle();
            return outp;
        }

        private static void AssertList(IReadOnlyList<string> actual, JsonElement exp, string ctx)
        {
            Assert.That(actual.Count, Is.EqualTo(exp.GetArrayLength()), ctx + " 數量");
            var i = 0;
            foreach (var item in exp.EnumerateArray())
            {
                if (actual[i] != item.GetString())
                    Assert.Fail($"{ctx}[{i}]：Kotlin={item.GetString()} / C#={actual[i]}（C# 全部={string.Join(",", actual)}）");
                i++;
            }
        }

        private static void AssertIntList(IReadOnlyList<int> actual, JsonElement exp, string ctx)
        {
            Assert.That(actual.Count, Is.EqualTo(exp.GetArrayLength()), ctx + " 數量");
            var i = 0;
            foreach (var item in exp.EnumerateArray())
            {
                if (actual[i] != item.GetInt32())
                    Assert.Fail($"{ctx}[{i}]：Kotlin={item.GetInt32()} / C#={actual[i]}（C# 全部={string.Join(",", actual)}）");
                i++;
            }
        }

        private void AssertOptClose(double? actual, JsonElement exp, string ctx)
        {
            if (exp.ValueKind == JsonValueKind.Null)
            {
                Assert.That(actual.HasValue, Is.False, ctx + "：應該是 null");
                return;
            }
            Assert.That(actual.HasValue, Is.True, ctx + "：不該是 null");
            AssertClose(actual.Value, exp.GetDouble(), ctx);
        }

        private void AssertClose(double actual, double expected, string ctx)
        {
            if (Math.Abs(actual - expected) <= _tolerance) return;
            Assert.Fail($"{ctx}：Kotlin={expected} / C#={actual}（Δ={Math.Abs(actual - expected)}）");
        }
    }
}
