using System;
using System.Collections.Generic;
using System.Text;

namespace BlueAgent.Logic
{
    /// <summary>
    /// Next 端界面文字的查表層（2026-10-28 完整 i18n）：一種語言一個扁平 JSON（`{"key": "文字"}`），
    /// 由 Unity 側 `NextStrings` 從 `Resources/i18n/next.{locale}.json` 讀進來交給這一支。
    ///
    /// 語意：
    /// <list type="bullet">
    /// <item>這一種語言缺字 ⇒ 退回正本語言（<see cref="Canonical"/>＝日語），再缺才**原樣回 key**
    ///   （畫面上看得到 key 比空白好查）；</item>
    /// <item>`{name}` 佔位符用 <see cref="Format"/> 代換，**沒給的留原樣**；</item>
    /// <item>伺服器的錯誤碼／HTTP 狀態只在這裡換成 key（<see cref="ErrorKey"/>），畫面不直接顯示 `trpg_unavailable` 這類碼。</item>
    /// </list>
    /// 放在 Logic 而不是 Unity 側：查表、退回、代換、語言判斷都是純函式，可以在 `dotnet test` 裡釘住。
    /// </summary>
    public sealed class NextText
    {
        public const string Ja = "ja";
        public const string ZhHans = "zh-Hans";
        public const string ZhHant = "zh-Hant";

        /// <summary>支援的語言（設定頁切換的順序）。</summary>
        public static readonly string[] Locales = { Ja, ZhHans, ZhHant };

        /// <summary>正本語言：缺字時退回它。</summary>
        public const string Canonical = Ja;

        private readonly Dictionary<string, Dictionary<string, string>> _tables;

        private NextText(Dictionary<string, Dictionary<string, string>> tables)
        {
            _tables = tables;
        }

        /// <summary>沒有任何表（資產讀不到）：每一條都原樣回 key。</summary>
        public static readonly NextText Empty = new NextText(new Dictionary<string, Dictionary<string, string>>());

        /// <summary>
        /// `{locale: JSON 原文}` → 查表器。壞掉的那一份當作空表（不丟例外：少一種語言還能退回正本）。
        /// 非字串的值丟掉。
        /// </summary>
        public static NextText Parse(IDictionary<string, string> jsonByLocale)
        {
            var tables = new Dictionary<string, Dictionary<string, string>>();
            if (jsonByLocale == null) return new NextText(tables);
            foreach (var pair in jsonByLocale)
            {
                var table = new Dictionary<string, string>();
                try
                {
                    var root = new JObject(pair.Value ?? "");
                    foreach (var key in root.Keys())
                    {
                        if (root.IsNull(key) || root.OptJObject(key) != null || root.OptJArray(key) != null) continue;
                        table[key] = root.OptString(key);
                    }
                }
                catch (Exception)
                {
                    table.Clear();
                }
                tables[pair.Key] = table;
            }
            return new NextText(tables);
        }

        /// <summary>認得的語言原樣回；其他（含空字串）一律當正本語言。</summary>
        public static string Normalize(string locale)
        {
            return locale == ZhHans || locale == ZhHant || locale == Ja ? locale : Canonical;
        }

        /// <summary>設定頁「換語言」：照 <see cref="Locales"/> 的順序輪下一個。</summary>
        public static string NextLocale(string locale)
        {
            var index = Array.IndexOf(Locales, Normalize(locale));
            return Locales[(index + 1) % Locales.Length];
        }

        /// <summary>每一種語言用它自己的寫法標名字（不翻譯：讓看不懂當前語言的人也找得到自己的）。</summary>
        public static string Label(string locale)
        {
            switch (Normalize(locale))
            {
                case ZhHans: return "简体中文";
                case ZhHant: return "繁體中文";
                default: return "日本語";
            }
        }

        /// <summary>
        /// 第一次啟動時跟系統語言（Unity `Application.systemLanguage` 的列舉名）。
        /// 只有中文分簡繁；籠統的 `Chinese` 當簡體。其他語言一律日語（角色本身說日語）。
        /// </summary>
        public static string FromSystemLanguage(string systemLanguage)
        {
            switch (systemLanguage ?? "")
            {
                case "ChineseTraditional": return ZhHant;
                case "ChineseSimplified":
                case "Chinese": return ZhHans;
                default: return Ja;
            }
        }

        public bool Has(string key)
        {
            return _tables.TryGetValue(Canonical, out var table) && table.ContainsKey(key ?? "");
        }

        public string Get(string key, string locale)
        {
            key = key ?? "";
            if (_tables.TryGetValue(Normalize(locale), out var table) && table.TryGetValue(key, out var text)
                && !string.IsNullOrEmpty(text)) return text;
            if (_tables.TryGetValue(Canonical, out var canon) && canon.TryGetValue(key, out var fallback)
                && !string.IsNullOrEmpty(fallback)) return fallback;
            return key;
        }

        /// <summary>`{name}` → 值；`args` 是「名字、值、名字、值…」交錯排；沒給的佔位符留原樣。</summary>
        public static string Format(string template, params object[] args)
        {
            if (string.IsNullOrEmpty(template) || args == null || args.Length < 2) return template ?? "";
            var values = new Dictionary<string, string>();
            for (var i = 0; i + 1 < args.Length; i += 2)
                values[Convert.ToString(args[i]) ?? ""] = Convert.ToString(args[i + 1]) ?? "";
            var output = new StringBuilder(template.Length + 16);
            var at = 0;
            while (at < template.Length)
            {
                var open = template.IndexOf('{', at);
                if (open < 0) break;
                var close = template.IndexOf('}', open + 1);
                if (close < 0) break;
                output.Append(template, at, open - at);
                var name = template.Substring(open + 1, close - open - 1);
                if (values.TryGetValue(name, out var value)) output.Append(value);
                else output.Append(template, open, close - open + 1);
                at = close + 1;
            }
            output.Append(template, at, template.Length - at);
            return output.ToString();
        }

        /// <summary>
        /// `NextApi` 串流（`/api/turn`、`/api/audio`）失敗時交上來的是**開發者看的原文**：
        /// `turn 503 {"error":"stt_unavailable"}`、`audio transport: Received no data…`。
        /// 這支把它拆回 (HTTP 狀態, 錯誤碼)，交給 <see cref="ErrorKey"/> 翻成人話；連不上＝狀態 0。
        /// 不是這兩種格式（已經是人話）⇒ 回 false，呼叫端原樣顯示。
        /// </summary>
        /// <summary>
        /// 通話那一通沒了（410）的原因是不是「被另一端接管」（後撥的蓋掉先撥的；伺服器回 `taken_over`，
        /// 其他——空閒超時、她掛、主人掛——是 `call_ended`）。WO-mr-call-and-switch §7 W3：兩端都要分開說。
        /// </summary>
        public static bool TakenOver(string code)
        {
            return code == "taken_over";
        }

        /// <summary>
        /// 一句送不出去的原文（`audio 410 {"error":"taken_over"}`）是不是「那一通已經沒了」；是的話 <paramref name="takenOver"/>
        /// 說是不是被另一端接管的。
        /// </summary>
        public static bool CallGone(string failure, out bool takenOver)
        {
            takenOver = false;
            if (!TryParseStreamFailure(failure, out var status, out var code) || status != 410) return false;
            takenOver = TakenOver(code);
            return true;
        }

        public static bool TryParseStreamFailure(string failure, out int status, out string code)
        {
            status = -1;
            code = "";
            if (string.IsNullOrEmpty(failure)) return false;
            var parts = failure.Split(new[] { ' ' }, 3);
            if (parts.Length < 2 || (parts[0] != "turn" && parts[0] != "audio")) return false;
            if (parts[1] == "transport:") { status = 0; return true; }
            if (!int.TryParse(parts[1], out status)) { status = -1; return false; }
            if (parts.Length == 3)
            {
                try { code = new JObject(parts[2]).OptString("error"); }
                catch (Exception) { code = ""; }
            }
            return true;
        }

        /// <summary>
        /// SSE `error` 事件的錯誤碼：串流層的例外放在頂層 `error`；模型那一層的錯（`first_token_timeout`／
        /// `network_error`…）是一個 frame，碼在 `payload.error`（2026-11-08 Quest 實機：只讀頂層 ⇒ 理由永遠是空的）。
        /// </summary>
        public static string StreamErrorCode(JObject data)
        {
            if (data == null) return "";
            var code = data.OptString("error");
            if (!string.IsNullOrEmpty(code)) return code;
            var payload = data.OptJObject("payload");
            return payload != null ? payload.OptString("error") : "";
        }

        /// <summary>給 log 看的完整理由：碼＋`payload.text` 的細節（畫面只顯示碼的說法）。</summary>
        public static string StreamErrorDetail(JObject data)
        {
            var payload = data != null ? data.OptJObject("payload") : null;
            var detail = payload != null ? payload.OptString("text") : "";
            var code = StreamErrorCode(data);
            return string.IsNullOrEmpty(detail) ? code : code + "：" + detail;
        }

        /// <summary>
        /// 這個串流失敗是不是「server 明確拒收」（HTTP 4xx／5xx）：這種失敗這一輪根本沒開，
        /// 錄音留著重送不會重複。連線斷在半路（狀態 0）、看不懂的格式都不算——不知道 server 收到沒有。
        /// </summary>
        public static bool ServerRejected(string failure)
        {
            return TryParseStreamFailure(failure, out var status, out _) && status >= 400;
        }

        /// <summary>
        /// 一個失敗的請求 → 要顯示的那條 key（再交給 <see cref="Get"/> 與 <see cref="Format"/>，佔位符是 `{status}`／`{code}`）。
        /// 先看伺服器的錯誤碼有沒有專屬說法（`err_code_*`），再看 HTTP 狀態（`err_http_*`），都沒有就是通用那條。
        /// </summary>
        public string ErrorKey(int status, string code)
        {
            if (status == 0) return "err_offline";
            if (!string.IsNullOrEmpty(code))
            {
                var byCode = "err_code_" + code;
                if (Has(byCode)) return byCode;
            }
            var byStatus = "err_http_" + status;
            if (Has(byStatus)) return byStatus;
            return status >= 500 ? "err_http_5xx" : "err_http_other";
        }
    }
}
