using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using BlueAgent.Logic.Engine;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>
    /// **fixture 對拍（M4：引擎語意層）**：`fixtures/engine_facs_mixer.json` 是 Kotlin 那一側
    /// （`blueagent/live2d/android/engine` 的 `EngineFixtureExportTest`）**自己在裝置上跑的那份引擎**
    /// 錄出來的軌跡——開發場（`live2d-native`）已由 owner 移除，那 41 支對拍測試與 18 MB fixture 不在，
    /// 所以真值＝自錄（`HANDOFF-20261012a_unity-m4.md` §3.2／§3.3）。
    ///
    /// 形狀：`cases[].frames[].in` ＝ 要餵進 `MotionMixer.mix` 的六層（缺席＝null）、`out` ＝ 當幀結果；
    /// `ops[]` ＝ `Facs` 那族純函式的逐條輸入輸出。浮點比對用 fixture header 的 `tolerance`（1e-4）。
    ///
    /// fixture 是**產物**，不要手改：要加 case 改 Kotlin 那支再重跑（`tools/export_fixtures.sh`）。
    /// </summary>
    [TestFixture]
    public class EngineFacsMixerFixtureTest
    {
        private static readonly string[] LayerNames =
            { "idle", "emotion", "reaction", "speech", "speechPerformance", "manual" };

        private JsonDocument _fixture;
        private float _tolerance;
        private readonly MotionMixer _mixer = new MotionMixer();

        [SetUp]
        public void Load()
        {
            var path = Path.Combine(AppContext.BaseDirectory, "fixtures", "engine_facs_mixer.json");
            Assert.That(File.Exists(path), Is.True,
                $"找不到 fixture：{path}（先在 Kotlin 側跑 `gradle :engine:test`）");
            _fixture = JsonDocument.Parse(File.ReadAllText(path));
            _tolerance = _fixture.RootElement.GetProperty("tolerance").GetSingle();
            Assert.That(_tolerance, Is.GreaterThan(0f), "fixture header 少了 tolerance——浮點比對沒有判準");
        }

        [TearDown]
        public void Unload() => _fixture?.Dispose();

        [Test]
        public void 逐幀重放六層混合並比對軌跡()
        {
            var cases = _fixture.RootElement.GetProperty("cases");
            var frames = 0;
            foreach (var c in cases.EnumerateArray())
            {
                var name = c.GetProperty("name").GetString();
                foreach (var frame in c.GetProperty("frames").EnumerateArray())
                {
                    var layers = new Dictionary<string, float>[6];
                    var inEl = frame.GetProperty("in");
                    for (var i = 0; i < LayerNames.Length; i++)
                    {
                        layers[i] = inEl.TryGetProperty(LayerNames[i], out var el) ? ReadMap(el) : null;
                    }

                    var actual = _mixer.Mix(
                        layers[0], layers[1], layers[2], layers[3], layers[4], layers[5]);

                    var t = frame.GetProperty("t").GetSingle();
                    AssertMap(actual, frame.GetProperty("out"), $"[{name}] t={t}");
                    frames++;
                }
            }
            Assert.That(frames, Is.GreaterThan(0), "fixture 裡一幀都沒有");
            TestContext.Out.WriteLine($"engine_facs_mixer.json：{cases.GetArrayLength()} 條情境／{frames} 幀全數相同（tol={_tolerance})");
        }

        [Test]
        public void 逐條重放Facs純函式()
        {
            var ops = _fixture.RootElement.GetProperty("ops");
            var total = 0;
            foreach (var o in ops.EnumerateArray())
            {
                var name = o.GetProperty("name").GetString();
                var op = o.GetProperty("op");
                var kind = op.GetProperty("kind").GetString();
                Dictionary<string, float> actual;

                switch (kind)
                {
                    case "createDefaultState":
                        actual = Facs.CreateDefaultState();
                        break;
                    case "clampState":
                        actual = Facs.ClampState(ReadMap(op.GetProperty("state")));
                        break;
                    case "add":
                        actual = Facs.Add(ReadMap(op.GetProperty("base")), ReadMap(op.GetProperty("overlay")),
                            op.TryGetProperty("weight", out var w) ? w.GetSingle() : 1f);
                        break;
                    case "scale":
                        actual = Facs.Scale(ReadMap(op.GetProperty("state")), op.GetProperty("scale").GetSingle());
                        break;
                    case "merge":
                        actual = Facs.Merge(ReadMap(op.GetProperty("base")), ReadMap(op.GetProperty("overlay")));
                        break;
                    case "scaleFromNeutral":
                        actual = Facs.ScaleFromNeutral(ReadMap(op.GetProperty("state")),
                            op.GetProperty("scale").GetSingle());
                        break;
                    case "interpolate":
                        actual = Facs.Interpolate(ReadMap(op.GetProperty("from")), ReadMap(op.GetProperty("to")),
                            op.GetProperty("t").GetSingle());
                        break;
                    case "normalize":
                        actual = Facs.Normalize(ReadMap(op.GetProperty("partial")));
                        break;
                    default:
                        Assert.Fail($"[{name}] 不認得的 kind={kind}（Kotlin 側新增了 case，C# 這裡還沒跟上）");
                        return;
                }

                AssertMap(actual, o.GetProperty("out"), $"[{name}]");
                total++;
            }
            TestContext.Out.WriteLine($"Facs ops：{total} 條全數相同");
        }

        /// <summary>只讀「真的有」的鍵——缺席與 0 是不同語意，不能補齊。</summary>
        private static Dictionary<string, float> ReadMap(JsonElement el)
        {
            var outp = new Dictionary<string, float>();
            foreach (var p in el.EnumerateObject()) outp[p.Name] = p.Value.GetSingle();
            return outp;
        }

        private void AssertMap(IDictionary<string, float> actual, JsonElement expected, string ctx)
        {
            var exp = ReadMap(expected);
            Assert.That(actual.Count, Is.EqualTo(exp.Count),
                $"{ctx}：鍵數不符（C#={actual.Count} / Kotlin={exp.Count}）");
            foreach (var kv in exp)
            {
                Assert.That(actual.ContainsKey(kv.Key), Is.True, $"{ctx}：C# 少了鍵 {kv.Key}");
                var a = actual[kv.Key];
                Assert.That(Math.Abs(a - kv.Value), Is.LessThanOrEqualTo(_tolerance),
                    $"{ctx}：{kv.Key} Kotlin={kv.Value} / C#={a}（Δ={Math.Abs(a - kv.Value)}）");
            }
        }
    }
}
