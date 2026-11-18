namespace BlueAgent.Logic
{
    /// <summary>鍵值落盤的接縫（Unity 那側是 PlayerPrefs；測試給記憶體版）。</summary>
    public interface IPrefs
    {
        /// <summary>沒有這個鍵 ⇒ 回空字串（不回 null）。</summary>
        string Get(string key);
        void Set(string key, string value);
        void Delete(string key);
    }

    /// <summary>
    /// `GET /api/events` 的 `Last-Event-ID` 游標——**按「伺服器＋身份」分區**（unity-review U-1，2026-10-28）。
    ///
    /// 以前整台只有一顆 `next.lastEventId`：伺服器的事件編號是**全域**遞增（不分 principal），
    /// 重播卻只回「這個人、id &gt; 游標」的那幾條 ⇒ owner 收到 3 之後換 guest，guest 帶著 3 去問，
    /// guest 自己還沒收到的 1、2 就被濾掉、再也補不回來。換一台伺服器（編號各自從頭算）也一樣。
    ///
    /// 用法：`/api/me` 成功、確定是誰之後 <see cref="Bind"/>；登出 <see cref="Unbind"/>
    /// （只清**執行期**這一份，各身份落盤的游標留著——下次那個人登回來要接著補）。
    /// </summary>
    public sealed class EventCursors
    {
        /// <summary>舊版的全域鍵：不知道屬於誰 ⇒ 綁定時直接丟掉（寧可這一次不補，也不要拿別人的去濾）。</summary>
        public const string LegacyKey = "next.lastEventId";
        // S3 改用 App Server 的獨立事件命名空間；舊 brain event id 絕不能拿到 /v1/events 重放。
        private const string Prefix = "next.appEventId|";

        private readonly IPrefs _prefs;
        private string _key = "";

        public EventCursors(IPrefs prefs)
        {
            _prefs = prefs;
        }

        /// <summary>目前這個身份的游標（沒綁定／沒收過 ⇒ 空字串 ⇒ 不帶 `Last-Event-ID`）。</summary>
        public string Current { get; private set; } = "";

        public bool Bound { get { return _key.Length > 0; } }

        /// <summary>`https://Host:8443/` 與 `https://host:8443` 是同一台。</summary>
        public static string NormalizeBase(string baseUrl)
        {
            return (baseUrl ?? "").Trim().TrimEnd('/').ToLowerInvariant();
        }

        /// <summary>落盤的鍵；伺服器或身份缺一個 ⇒ 空字串（不存、不帶）。</summary>
        public static string KeyFor(string baseUrl, string principal)
        {
            var server = NormalizeBase(baseUrl);
            var who = (principal ?? "").Trim();
            if (server.Length == 0 || who.Length == 0) return "";
            return Prefix + server + "|" + who;
        }

        public void Bind(string baseUrl, string principal)
        {
            _key = KeyFor(baseUrl, principal);
            Current = _key.Length == 0 ? "" : (_prefs.Get(_key) ?? "");
            _prefs.Delete(LegacyKey);
        }

        public void Unbind()
        {
            _key = "";
            Current = "";
        }

        /// <summary>服務端明確說游標已失效：清掉後從可讀事件起點重同步，不能帶同一 id 無限重連。</summary>
        public void Reset()
        {
            if (Bound) _prefs.Delete(_key);
            Current = "";
        }

        /// <summary>收到一條帶 `id:` 的事件。沒綁定時不記（登入前／登出後收到的不屬於任何人）。</summary>
        public void Advance(string id)
        {
            if (!Bound || string.IsNullOrEmpty(id) || id == Current) return;
            Current = id;
            _prefs.Set(_key, id);
        }
    }
}
