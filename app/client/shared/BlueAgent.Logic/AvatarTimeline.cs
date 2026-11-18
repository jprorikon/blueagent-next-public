using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text.RegularExpressions;

namespace BlueAgent.Logic
{
    /// <summary>
    /// **動態立繪**——真相 <c>assets/webui/avatar.js</c>（154 行）。
    ///
    /// 剝除 live2d 之後，「**切角色時換立繪**、說話時嘴型動」就是這一支在管；也是 owner
    /// 2026-10-16 說的「其他角色走和 webui 一樣的邏輯」那一條。
    ///
    /// avatar.js 的檔頭寫得很明白：**這四條規則是用 owner 的 bug 報告換來的，不准重新推導**：
    ///   1. **嘴型框是瞬間換**（`transitionMs = 0`）——兩個嘴型框交叉淡入會把嘴唇**疊影**、
    ///      看起來像閃一下；
    ///   2. `animations.speaking.frames` 是**查表**（按檔名比對），**不是輪播**；
    ///   3. `closed` 畫 `images.normal`；**表上缺的那一行就什麼都不畫**（安靜地留著原圖）；
    ///   4. 沒有音訊時，`speaking` 狀態**停在 `images.speaking`** 那一張。
    ///
    /// 另外兩條（`sync` 的細節）：同一個 `frames` 集合重複 `setState` **不重啟動畫**（memo by key）；
    /// `imageState` 在 memo 判斷**之前**就寫掉。
    ///
    /// 時間軸：`normal`／`thinking` 按 `frames[i].durationMs` 輪播（缺值預設 167）；
    /// `speaking` 進來就**停**在代表框等 `lip` 訊息。
    ///
    /// **這一支是純邏輯**：只回答「現在該畫哪一張檔名」（<c>null</c>＝不畫）。
    /// 兩顆 `img` 疊著交叉淡（avatar.js 的 front/back）與淡入淡出留給畫面層；
    /// 這裡只把那一輪第一張的淡入時間（<see cref="TransitionMs"/>）交出去。
    /// </summary>
    public sealed class AvatarTimeline
    {
        public const string Normal = "normal";
        public const string Thinking = "thinking";
        public const string Speaking = "speaking";
        public const string Closed = "closed";

        /// <summary>`SHAPES = ['a','i','u','e','o']`。</summary>
        public static readonly string[] Shapes = { "a", "i", "u", "e", "o" };

        /// <summary>嘴型的檔名長相：`animated-mouth-a.png`／`animated-mouth-u-v2.png`。</summary>
        static readonly Regex MouthFile =
            new Regex(@"animated-mouth-([a-z]+?)(?:-v\d+)?\.png$", RegexOptions.CultureInvariant);

        sealed class Frame
        {
            public Frame(string src, double durationMs) { Src = src; DurationMs = durationMs; }
            public string Src { get; }
            public double DurationMs { get; }
        }

        readonly List<JObject> _profiles = new List<JObject>();
        readonly List<Frame> _frames = new List<Frame>();

        string _characterId;
        string _state = Normal;
        string _key = "";
        bool _lipDriven;
        string _src;
        int _index;
        double _frameStartedAt;
        double _nowMs;
        double _transitionMs;

        /// <summary>由角色清單（`profiles`）建；<paramref name="manifestJson"/> 是**角色卡**的原始 JSON。</summary>
        public AvatarTimeline(string manifestJson, string characterId)
            : this(Manifests(manifestJson), characterId)
        {
        }

        public AvatarTimeline(List<JObject> profiles, string characterId)
        {
            if (profiles != null) _profiles.AddRange(profiles);
            _characterId = characterId ?? "";
            SetState(Normal);
        }

        /// <summary>現在畫的是哪一張（相對於頁面的相對路徑；<c>null</c>＝不畫）。</summary>
        public string Src => _src;

        /// <summary>`normal`／`thinking`／`speaking`。</summary>
        public string State => _state;

        /// <summary>這一輪第一張的淡入時間（毫秒）；嘴型框永遠是 0。</summary>
        public double TransitionMs => _transitionMs;

        /// <summary>嘴型已經被 `lip` 接管的旗標（avatar.js `lipDriven`）。</summary>
        public bool LipDriven => _lipDriven;

        /// <summary>現在這個 id 在清單裡有沒有對應的角色卡（沒有時 avatar.js 會退回第一張卡）。</summary>
        public bool HasProfile => Profile() != null;

        public static bool IsKnownState(string state) =>
            state == Normal || state == Thinking || state == Speaking;

        /// <summary>換角色（avatar.js `setCharacter`）：memo 與 lip 旗標都重設，回到 `normal`。</summary>
        public void SetCharacter(string id)
        {
            _characterId = id ?? "";
            _key = "";
            _lipDriven = false;
            SetState(Normal);
        }

        /// <summary>換狀態（avatar.js `setState`／`sync`）。</summary>
        public void SetState(string state)
        {
            if (string.IsNullOrEmpty(state)) state = Normal;
            _state = state;                       // avatar.js 先把 imageState 寫掉，才比對 memo
            var profile = Profile();
            if (profile == null) { _src = null; _frames.Clear(); _key = ""; return; }

            var animations = Obj(profile, "animations");
            var animation = Obj(animations, state);
            var images = Obj(profile, "images");
            var fallback = Str(images, state);
            if (fallback.Length == 0) fallback = Str(images, Normal);
            var frames = Frames(animation, fallback);

            var key = Str(profile, "id") + "/" + state + "/" + FramesKey(frames);
            if (key == _key) return;             // 同一個 frames 集合 ⇒ 不重啟動畫
            _key = key;

            _transitionMs = Num(animation, "transition_ms") ?? Num(animation, "transitionMs") ?? 0;
            _frames.Clear();
            _frames.AddRange(frames);
            _index = 0;
            _frameStartedAt = _nowMs;

            if (state == Speaking)
            {
                // **查表、不輪播**：停在代表框，等 `lip` 訊息（規則 2＋4）。
                _src = fallback.Length != 0 ? fallback : null;
                return;
            }

            _src = _frames.Count != 0 ? _frames[0].Src : null;
        }

        /// <summary>
        /// 只有 <c>speaking</c> 才吃嘴型；形狀限 a／i／u／e／o／closed。
        /// 回 <c>true</c>＝這一格有圖可畫（表上缺那一行 ⇒ 回 <c>false</c>，**畫面留在原圖**）。
        /// </summary>
        public bool ApplyLip(string mouth)
        {
            // avatar.js：imageState !== 'speaking' ⇒ 直接不收
            if (_state != Speaking) return false;
            if (!IsShape(mouth) && mouth != Closed) return false;
            var profile = Profile();
            if (profile == null) return false;

            _lipDriven = true;                   // 先設，才去找那一行（缺行也會留在 true）
            var row = FindMouthRow(Arr(Obj(Obj(profile, "animations"), Speaking), "frames"), mouth);
            var src = mouth == Closed ? Str(Obj(profile, "images"), Normal) : SrcOf(row);
            if (src.Length == 0) return false;   // 規則 3：缺的那一行什麼都不畫
            _src = src;                          // 規則 1：**瞬間**（不做交叉淡）
            _transitionMs = 0;
            return true;
        }

        /// <summary>
        /// 推時間（毫秒，單調）。`speaking` 停住不動；其餘按 `durationMs` 輪播（規則：預設 167）。
        /// **呼叫前請先給一個基準現在時刻**（第一次 <see cref="SetState"/> 就是用它當起點）。
        /// </summary>
        public void Advance(double nowMs)
        {
            _nowMs = nowMs;
            if (_state == Speaking) return;      // 停在代表框／嘴型上
            if (_frames.Count == 0) { _src = null; return; }
            if (_src == null) _src = _frames[0].Src;

            var guard = 0;
            while (nowMs - _frameStartedAt >= _frames[_index].DurationMs && guard++ < 100000)
            {
                _frameStartedAt += _frames[_index].DurationMs;
                _index = (_index + 1) % _frames.Count;
                _src = _frames[_index].Src;
            }
        }

        /// <summary>停止（avatar.js `stop`）：畫面層拿這個把計時器停掉；純邏輯這邊只清狀態。</summary>
        public void Stop()
        {
            _frames.Clear();
            _key = "";
            _src = null;
        }

        // ── 內部 ─────────────────────────────────────────────────────────────

        static bool IsShape(string mouth)
        {
            foreach (var shape in Shapes) if (shape == mouth) return true;
            return false;
        }

        JObject Profile()
        {
            foreach (var p in _profiles) if (Str(p, "id") == _characterId) return p;
            return _profiles.Count != 0 ? _profiles[0] : null;   // avatar.js：`|| list()[0]`
        }

        static string SrcOf(JObject frame)
        {
            if (frame == null) return "";
            var src = Str(frame, "src");
            return src.Length != 0 ? src : Str(frame, "file");
        }

        static JObject FindMouthRow(List<JObject> rows, string mouth)
        {
            foreach (var row in rows)
            {
                var src = SrcOf(row);
                if (src.Length == 0) continue;
                var match = MouthFile.Match(src);
                if (match.Success && match.Groups[1].Value == mouth) return row;
            }
            return null;
        }

        static List<Frame> Frames(JObject animation, string fallback)
        {
            var outp = new List<Frame>();
            foreach (var f in Arr(animation, "frames"))
            {
                var src = SrcOf(f);
                if (src.Length == 0) continue;
                var duration = Num(f, "durationMs") ?? Num(f, "duration_ms") ?? 167;
                outp.Add(new Frame(src, duration));
            }
            if (outp.Count == 0 && fallback.Length != 0) outp.Add(new Frame(fallback, 1000));
            return outp;
        }

        /// <summary>memo 用的鍵（avatar.js：`JSON.stringify(frames)` 那一份的等價物）。</summary>
        static string FramesKey(List<Frame> frames)
        {
            var parts = new List<string>();
            foreach (var f in frames) parts.Add(f.Src + ":" + f.DurationMs.ToString("R", CultureInfo.InvariantCulture));
            return string.Join("|", parts);
        }

        static List<JObject> Manifests(string json)
        {
            var outp = new List<JObject>();
            if (string.IsNullOrEmpty(json)) return outp;
            var trimmed = json.TrimStart();
            if (trimmed.StartsWith("["))
            {
                foreach (var item in new JArray(json).ItemsInternal)
                    if (item is JObjVal o) outp.Add(o.Value);
            }
            else if (trimmed.StartsWith("{"))
            {
                outp.Add(new JObject(json));
            }
            return outp;
        }

        // ── kotlinx `JsonObject` 的取值（與 `HomeState` 同一套嚴格寫法）────────

        static string Str(JObject o, string key) => o?.Raw(key) is JStrVal p ? p.Value : "";

        static JObject Obj(JObject o, string key) => o?.Raw(key) is JObjVal j ? j.Value : null;

        /// <summary>只有**非字串**的 primitive 算數（JSON null 的 content 是 `"null"` ⇒ 取不到）。</summary>
        static double? Num(JObject o, string key)
        {
            var v = o?.Raw(key);
            if (v == null || v is JObjVal || v is JArrVal || v is JStrVal) return null;
            return double.TryParse(v.Content(), NumberStyles.Float, CultureInfo.InvariantCulture, out var d)
                ? d : (double?)null;
        }

        static List<JObject> Arr(JObject o, string key)
        {
            var outp = new List<JObject>();
            if (o?.Raw(key) is JArrVal a)
            {
                foreach (var item in a.Value.ItemsInternal) if (item is JObjVal j) outp.Add(j.Value);
            }
            return outp;
        }
    }
}
