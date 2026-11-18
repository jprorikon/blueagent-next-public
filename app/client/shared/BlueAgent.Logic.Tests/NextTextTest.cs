using System.Collections.Generic;
using System.IO;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>
    /// Next 端界面文字的查表層（2026-10-28 完整 i18n）：退回、代換、系統語言、錯誤碼對應。
    /// 最後一組讀真的 `Resources/i18n/next_*.json`：錯誤路徑一定會落到的那幾條，三語都要有。
    /// </summary>
    public class NextTextTest
    {
        private static NextText Sample()
        {
            return NextText.Parse(new Dictionary<string, string>
            {
                { NextText.Ja, "{\"hello\":\"こんにちは\",\"only_ja\":\"日本語だけ\",\"err_http_404\":\"見つかりません\"," +
                               "\"err_code_turn_not_found\":\"もう残っていません\",\"err_http_5xx\":\"サーバーエラー（{status}）\"," +
                               "\"err_http_other\":\"エラー（{status} {code}）\",\"err_offline\":\"繋がりません\"}" },
                { NextText.ZhHans, "{\"hello\":\"你好\",\"err_http_404\":\"找不到\"}" },
                { NextText.ZhHant, "{ broken" },
            });
        }

        [Test]
        public void 照界面語言取字()
        {
            var text = Sample();
            Assert.That(text.Get("hello", NextText.ZhHans), Is.EqualTo("你好"));
            Assert.That(text.Get("hello", NextText.Ja), Is.EqualTo("こんにちは"));
        }

        [Test]
        public void 缺字退回日語_再缺回key()
        {
            var text = Sample();
            Assert.That(text.Get("only_ja", NextText.ZhHans), Is.EqualTo("日本語だけ"));
            Assert.That(text.Get("hello", NextText.ZhHant), Is.EqualTo("こんにちは"), "壞掉的那份當空表");
            Assert.That(text.Get("nope", NextText.ZhHans), Is.EqualTo("nope"));
            Assert.That(NextText.Empty.Get("hello", NextText.Ja), Is.EqualTo("hello"));
        }

        [Test]
        public void 不認得的語言當日語()
        {
            Assert.That(NextText.Normalize("en"), Is.EqualTo(NextText.Ja));
            Assert.That(NextText.Normalize(""), Is.EqualTo(NextText.Ja));
            Assert.That(NextText.Normalize(NextText.ZhHant), Is.EqualTo(NextText.ZhHant));
        }

        [Test]
        public void 佔位符代換_沒給的留原樣()
        {
            Assert.That(NextText.Format("{count} 件／{total}", "count", 3), Is.EqualTo("3 件／{total}"));
            Assert.That(NextText.Format("a{x}b{x}", "x", "-"), Is.EqualTo("a-b-"));
            Assert.That(NextText.Format("沒有佔位符"), Is.EqualTo("沒有佔位符"));
            Assert.That(NextText.Format("{未閉合", "x", 1), Is.EqualTo("{未閉合"));
        }

        [Test]
        public void 第一次啟動跟系統語言()
        {
            Assert.That(NextText.FromSystemLanguage("ChineseSimplified"), Is.EqualTo(NextText.ZhHans));
            Assert.That(NextText.FromSystemLanguage("ChineseTraditional"), Is.EqualTo(NextText.ZhHant));
            Assert.That(NextText.FromSystemLanguage("Chinese"), Is.EqualTo(NextText.ZhHans));
            Assert.That(NextText.FromSystemLanguage("Japanese"), Is.EqualTo(NextText.Ja));
            Assert.That(NextText.FromSystemLanguage("English"), Is.EqualTo(NextText.Ja));
        }

        [Test]
        public void 換語言照順序輪()
        {
            Assert.That(NextText.NextLocale(NextText.Ja), Is.EqualTo(NextText.ZhHans));
            Assert.That(NextText.NextLocale(NextText.ZhHans), Is.EqualTo(NextText.ZhHant));
            Assert.That(NextText.NextLocale(NextText.ZhHant), Is.EqualTo(NextText.Ja));
            Assert.That(NextText.Label(NextText.ZhHant), Is.EqualTo("繁體中文"));
        }

        [Test]
        public void 錯誤碼優先_其次HTTP狀態_最後通用()
        {
            var text = Sample();
            Assert.That(text.ErrorKey(0, ""), Is.EqualTo("err_offline"));
            Assert.That(text.ErrorKey(404, "turn_not_found"), Is.EqualTo("err_code_turn_not_found"));
            Assert.That(text.ErrorKey(404, "something_else"), Is.EqualTo("err_http_404"));
            Assert.That(text.ErrorKey(502, ""), Is.EqualTo("err_http_5xx"));
            Assert.That(text.ErrorKey(418, "teapot"), Is.EqualTo("err_http_other"));
        }

        [Test]
        public void 串流失敗原文拆回狀態與錯誤碼()
        {
            Assert.That(NextText.TryParseStreamFailure("audio transport: Received no data in response", out var s0, out var c0), Is.True);
            Assert.That((s0, c0), Is.EqualTo((0, "")));
            Assert.That(NextText.TryParseStreamFailure("turn 503 {\"error\":\"stt_unavailable\"}", out var s1, out var c1), Is.True);
            Assert.That((s1, c1), Is.EqualTo((503, "stt_unavailable")));
            Assert.That(NextText.TryParseStreamFailure("audio 429", out var s2, out var c2), Is.True);
            Assert.That((s2, c2), Is.EqualTo((429, "")));
            Assert.That(NextText.TryParseStreamFailure("turn 500 <html>oops", out var s3, out var c3), Is.True);
            Assert.That((s3, c3), Is.EqualTo((500, "")), "回包不是 JSON 也只取狀態");
            Assert.That(NextText.TryParseStreamFailure("サーバーに繋がりません", out _, out _), Is.False, "已經是人話的原樣顯示");
            Assert.That(NextText.TryParseStreamFailure("", out _, out _), Is.False);
        }

        [Test]
        public void 通話沒了分得出是不是被另一端接管()
        {
            Assert.That(NextText.TakenOver("taken_over"), Is.True);
            Assert.That(NextText.TakenOver("call_ended"), Is.False);
            Assert.That(NextText.TakenOver(""), Is.False);
            Assert.That(NextText.CallGone("audio 410 {\"error\":\"taken_over\"}", out var taken), Is.True);
            Assert.That(taken, Is.True);
            Assert.That(NextText.CallGone("audio 410 {\"error\":\"call_ended\"}", out taken), Is.True);
            Assert.That(taken, Is.False, "空閒超時／她掛／主人掛不能說成被接管");
            Assert.That(NextText.CallGone("audio 422 {\"error\":\"stt_failed\"}", out _), Is.False);
            Assert.That(NextText.CallGone("audio transport: Received no data in response", out _), Is.False);
        }

        [Test]
        public void 模型那一層的錯碼在payload裡()
        {
            var frame = new JObject("{\"kind\":\"error\",\"payload\":{\"error\":\"first_token_timeout\",\"text\":\"第一個字 1500 ms 內沒到\"}}");
            Assert.That(NextText.StreamErrorCode(frame), Is.EqualTo("first_token_timeout"),
                        "2026-11-08 Quest：只讀頂層 ⇒ 理由永遠是空的");
            Assert.That(NextText.StreamErrorDetail(frame), Is.EqualTo("first_token_timeout：第一個字 1500 ms 內沒到"));
            var top = new JObject("{\"error\":\"RuntimeError: boom\",\"turn_id\":\"t1\"}");
            Assert.That(NextText.StreamErrorCode(top), Is.EqualTo("RuntimeError: boom"));
            Assert.That(NextText.StreamErrorDetail(top), Is.EqualTo("RuntimeError: boom"));
            Assert.That(NextText.StreamErrorCode(null), Is.EqualTo(""));
        }

        [Test]
        public void 只有HTTP拒收才算沒開這一輪()
        {
            Assert.That(NextText.ServerRejected("audio 503 {\"error\":\"stt_unavailable\"}"), Is.True);
            Assert.That(NextText.ServerRejected("audio 429"), Is.True);
            Assert.That(NextText.ServerRejected("audio transport: Received no data in response"), Is.False,
                        "斷在半路＝不知道 server 收到沒有，不能留著讓人重送");
            Assert.That(NextText.ServerRejected("サーバーに繋がりません"), Is.False);
            Assert.That(NextText.ServerRejected(""), Is.False);
        }

        // ── 真檔 ──────────────────────────────────────────────────────────────

        private static NextText Real()
        {
            var sources = new Dictionary<string, string>();
            foreach (var locale in NextText.Locales)
            {
                var path = Path.Combine(System.AppContext.BaseDirectory, "i18n", "next_" + locale + ".json");
                Assert.That(File.Exists(path), Is.True, "缺語言檔：" + path);
                sources[locale] = File.ReadAllText(path);
            }
            return NextText.Parse(sources);
        }

        [Test]
        public void 真檔_錯誤路徑的通用那幾條三語都在()
        {
            var text = Real();
            foreach (var key in new[] { "err_offline", "err_http_5xx", "err_http_other", "err_unknown", "send", "settings" })
            {
                foreach (var locale in NextText.Locales)
                {
                    var value = text.Get(key, locale);
                    Assert.That(value, Is.Not.EqualTo(key), key + " 在 " + locale + " 缺字");
                }
            }
            Assert.That(text.Get("send", NextText.ZhHans), Is.Not.EqualTo(text.Get("send", NextText.Ja)),
                        "中文檔不能整份退回日語");
        }
    }
}
