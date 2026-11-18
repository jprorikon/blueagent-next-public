using System;
using System.Collections.Generic;
using System.Text;

namespace BlueAgent.Logic
{
    /// <summary>
    /// 呼びかけ語（wake phrase）の判定——`blueagent/live2d/android/kmp/src/commonMain/.../WakeWord.kt`
    /// の**逐行移植**（真値＝Kotlin 側）。等価性は `fixtures/wake_word.json`（Kotlin の
    /// `WakeWordFixtureExportTest` が機械生成）を逐條對拍して判定する。
    ///
    /// なぜ有るか：常時オンのマイクと、部屋の雑談に答えてしまう殻との間に立っているのはこれだけ。
    /// 卓上では名前を押したくない ⇒ シェルは自分の名前を聞き、続きをそのまま用件として扱う
    /// （「ミラ、今日は何をする？」が一息で一ターン。相槌の往復も二度目の発話も要らない）。
    ///
    /// 照合は**生の音声ではなく whisper の出力**に掛ける。理由は意図的な取引：転写のためモデルは
    /// 元々常駐していて、名前に対して小さいモデルは十分信頼でき、False start は所有者が状態列で
    /// 見て分かる。専用の KWS（openWakeWord／Porcupine）は**これで弱いと判ってから**で、学習済み
    /// モデルとライセンスのコストが要る。
    ///
    /// この型の存在意義は whisper の**形**を吸収すること：読点「、」を落とし、ひらがなとカタカナを
    /// 混ぜ、敬称を付けたし、たまに名前の前にフィラーを聞く。だから照合は**畳んだコピー**（ひらがな
    /// →カタカナ、空白なし、記号なし）で行い、用件そのものは**原文**から切り出す（句読点とかなを
    /// そのままエージェントに渡す）。
    ///
    /// 二つの規律が「ついでに彼女を起こす」のを防ぐ：
    ///  * 名前は**先頭**でなければならない（[FILLERS] のフィラーの後は可）⇒「そのミラは誰？」
    ///    （彼女の**噂**）は呼びかけではない；
    ///  * 敬称だけは**呼びかけであって用件ではない** ⇒「ミラちゃん」で起きて、そこで止まる。
    ///
    /// ⚠️ 移植陷阱 §1：空白判定は必ず <see cref="KotlinText.IsWhitespace"/>（Kotlin の
    /// `Char.isWhitespace()`。U+0085（NEL）／U+180E を**空白としない**）。**`char.IsWhiteSpace` は使わない**。
    /// 字母判定は `char.IsLetterOrDigit` を使う（.NET のそれは Letter ∪ DecimalDigitNumber ＝ Kotlin の
    /// `Char.isLetterOrDigit()` と一致する）。
    ///
    /// このモジュールに**時計は無い**（純関数）⇒ `IClock`／`FakeClock` を必要としない。
    /// </summary>
    public static class WakeWord
    {
        /// <summary>
        /// 設定が何も名乗らないときに角色が応える語。
        ///
        /// 漢字綴りは飾りではない：whisper は日本語を日本人の書き手のように書くので、「ミラ」は
        /// 実在の語として戻ってくる——「無月」（ミラ）と「松木」（まつき）は同じ午後にどちらも現れ、
        /// どちらもカタカナから 1 字違いではない ⇒ 寛容な比較では見えない。**1 字違いのカタカナ**
        /// （めつき／もつき）はここに書かなくてよい——それは [MIN_APPROXIMATE_LENGTH] の役目。
        /// </summary>
        private static readonly Dictionary<string, List<string>> DEFAULTS = new Dictionary<string, List<string>>
        {
            { "aster", new List<string> { "アスター", "里央", "アスター" } },
            { "mira", new List<string> { "ミラ", "無月", "松木" } },
            // nori（2026-10-07）：喚醒詞は「ノリ」。owner 裁定：裸の「ノリ」は単音・短すぎて不安定
            // ⇒「ノリ」を使う。よってここには「ちゃん」形の発音だけを置く（whisper が書く漢字異形含む）。
            // 裸の「ノリ」は**置かない**——短形は KWS のコーパスでは**ハード負例**（技能実測：短形を負例に
            // するのが誤報削減に一番効く）。テキスト経路で裸の「ノリ」を呼びかけ扱いすると同じ規則を壊す。
            { "nori", new List<string> { "ノリ", "ノリちゃん", "圭ちゃん", "恵ちゃん" } },
            // lumi（2026-10-18，Unity 版新增的 live2d 角色「ルミちゃん」）：同 nori，用「ちゃん」形（裸的「ルミ」太短）。
            // Kotlin 側 `WakeWord.kt` 尚無此角色（原 app 沒有她）。
            { "lumi", new List<string> { "ルミちゃん", "ルミちゃん", "海ちゃん" } },
        };

        /// <summary>人が名前に前置する語。この一覧が無ければ名前は先頭でなければならない。</summary>
        private static readonly List<string> FILLERS = new List<string>
        {
            "ねえ", "ねぇ", "ねー", "あの", "あのね", "ちょっと", "もしもし", "すみません", "やあ", "おい", "よう",
        };

        /// <summary>呼びかけの接尾語——この家では用件の頭にはならない。</summary>
        private static readonly List<string> VOCATIVE = new List<string> { "ちゃん", "さん", "くん", "様", "さま", "たん" };

        /// <summary>残り全部がこれらの一つなら、所有者は呼んだだけで何も言っていない。</summary>
        private static readonly List<string> IGNORABLE = Combine(VOCATIVE, "オーナー", "ね", "よ", "ー", "〜", "は", "さ");

        /// <summary>
        /// 会話を終わらせて発話権を呼びかけ語に返す語。
        ///
        /// それでも一ターンではある——他の何と同じように答える（それが自然な別れ）——ただ会話を
        /// 開いたままにしない。**発話の先頭だけ**で照合するので、「今日はもういい感じだったね」は
        /// 一つの文であり、「おやすみ」は別れ。
        /// </summary>
        private static readonly List<string> FAREWELL = new List<string>
        {
            "おやすみ", "おやすみなさい", "もういい", "もういいよ", "おしまい", "終わりにして",
            "またね", "またあした", "また明日", "さようなら", "ばいばい", "じゃあね",
        };

        /// <summary>
        /// 記号（畳むと落ち、用件の前後からも裁かれる）。Kotlin 側は `CharArray`、
        /// ここは同じ並びの `const string`（`IndexOf` の線形走査＝`in CharArray` と同じ）。
        /// </summary>
        private const string PUNCT = " \t\u3000、。，,.！!？?・「」『』（）()【】:：;；~〜ー-—…\"'";

        /// <summary>音声化は畳んで消す ⇒「が」と「か」は等しい——[FoldChar] を見よ。</summary>
        private const int MIN_APPROXIMATE_LENGTH = 3;

        /// <summary>
        /// 濁音／半濁音 → 清音。三組の「清音／濁音」文字列から生成し、ひらがなとカタカナを各一份
        /// （かな域のオフセットは固定 ＋0x60：か 0x304B → カ 0x30AB、ゔ 0x3094 → ヴ 0x30F4 ✓）。
        ///
        /// ⚠️ 2026-10-03 に `commonMain` へ移すとき書き換えた：元は `java.text.Normalizer` の NFD 分解
        /// （JVM 専有）だったが `commonMain` に Unicode 正規化は無い ⇒ [VOICING] の表にした。二経路は等価：
        ///  * 合成済みの字（が）⇒ 表が直接清音（か）に換える；
        ///  * 分解済みの字（か ＋ U+3099）⇒ その結合記号は [FoldChar] で落ちる（字母でも数字でもない）。
        /// </summary>
        private static readonly Dictionary<char, char> VOICING = BuildVoicing();

        private static readonly char[] SplitChars = { ',', '、', ';', '；', '\n' };

        /// <summary>一つの掛け声の結果。<see cref="Rest"/> は生の残り（原文の綴りのまま）。</summary>
        public sealed class Hit
        {
            public Hit(string phrase, string rest)
            {
                Phrase = phrase;
                Rest = rest;
            }

            public string Phrase { get; }

            public string Rest { get; }

            /// <summary>裸の呼びかけでは false：シェルは答えて次の発話を待つ。</summary>
            public bool HasRequest => Rest.Length > 0;
        }

        /// <summary>畳んだ文字列と、畳んだ各文字が原文のどこから来たかの対応表。</summary>
        public sealed class Folded
        {
            public Folded(string text, int[] orig)
            {
                Text = text;
                Orig = orig;
            }

            public string Text { get; }

            /// <summary>畳んだ i 文字目は原文の <c>Orig[i]</c> 文字目（UTF-16 符号単位）。</summary>
            public int[] Orig { get; }
        }

        /// <summary>この角色の掛け声の語：設定の自前の一覧が勝ち、無ければ [DEFAULTS]。</summary>
        public static List<string> Phrases(string character, string custom)
        {
            var own = new List<string>();
            // `custom.split(',', '、', ';', '；', '\n')`——連続する区切りは**空文字を生む**（Kotlin も同じ）。
            // それを下の filter が落とす。
            foreach (var part in custom.Split(SplitChars, StringSplitOptions.None))
            {
                var trimmed = KotlinText.Trim(part);
                if (trimmed.Length > 0) own.Add(trimmed);
            }
            if (own.Count > 0) return own;
            // `character.trim().lowercase()`——trim は Kotlin の空白、lowercase は locale 非依存。
            return DEFAULTS.TryGetValue(KotlinText.Trim(character).ToLowerInvariant(), out var def)
                ? def
                : new List<string>();
        }

        /// <summary>状態列に読ませる形：`ミラ`（或は `ミラ / ミラ`）。</summary>
        public static string Label(string character, string custom)
        {
            var list = Phrases(character, custom);
            return list.Count == 0 ? "（未設定）" : string.Join(" / ", list);
        }

        /// <summary>
        /// 会話の終わりか。発話の**先頭だけ**が数えられ、[FAREWELL] のどれか一つだけ——
        /// そうでないと「もういい」を含むあらゆる文が所有者の足元で発話権を閉じてしまう。
        /// </summary>
        public static bool IsFarewell(string text)
        {
            var folded = Fold(text).Text;
            if (folded.Length == 0) return false;
            foreach (var word in FAREWELL)
            {
                if (folded.StartsWith(Fold(word).Text, StringComparison.Ordinal)) return true;
            }
            return false;
        }

        /// <summary>
        /// 転写の中から呼びかけを見つける。このシェル宛でなければ `null`。
        ///
        /// **惜しい一致も数える**：whisper の小さいモデルはカタカナの名前を日本語の音韻を通して聞く
        /// （最初に電話で測ったとき「ミラ」は「めつき」で戻ってきた）⇒ 3 文字以上の語は 1 文字違いを許す。
        /// 有声化は差ですらない（[Fold] が濁点を落とす）ので、置換の他に ムツギ／ムヅキ も覆う。
        /// </summary>
        public static Hit Match(string text, IReadOnlyList<string> phrases)
        {
            var folded = Fold(text);
            if (folded.Text.Length == 0) return null;
            foreach (var phrase in phrases)
            {
                var needle = Fold(phrase).Text;
                if (needle.Length == 0) continue;
                var span = Locate(folded.Text, needle);
                if (!span.HasValue) continue;
                var lastFolded = span.Value.At + span.Value.Len - 1;
                if (lastFolded >= folded.Orig.Length) continue;
                var after = folded.Orig[lastFolded] + 1;
                var rest = after <= text.Length ? text.Substring(after) : "";
                return new Hit(phrase, Clean(rest));
            }
            return null;
        }

        /// <summary>[Match] が比較するのと同じ形に割る——単体テストのために公開する。</summary>
        public static string Normalized(string text) => Fold(text).Text;

        /// <summary>
        /// whisper の綴りは安定しない：「ミラ」が「ミラ」で戻り、後ろに「、」が付き、全角空白が
        /// 混ざることもある。比較可能な形に畳み、原文の位置を覚えておく。
        /// </summary>
        public static Folded Fold(string text)
        {
            var sb = new StringBuilder(text.Length);
            var map = new List<int>(text.Length);
            // Kotlin の `text.withIndex()` と同じく UTF-16 符号単位で回す（char ＝ Kotlin の Char）。
            for (var i = 0; i < text.Length; i++)
            {
                var folded = FoldChar(text[i]);
                if (!folded.HasValue) continue;
                sb.Append(folded.Value);
                map.Add(i);
            }
            return new Folded(sb.ToString(), map.ToArray());
        }

        // ── 内部 ────────────────────────────────────────────────────────────

        /// <summary>名前がどこから始まり、畳んだ文字を何個にわたるか。見つからなければ null。</summary>
        private static (int At, int Len)? Locate(string folded, string needle)
        {
            var starts = new List<int>();
            foreach (var filler in FILLERS)
            {
                var lead = Fold(filler).Text;
                if (folded.StartsWith(lead, StringComparison.Ordinal)) starts.Add(lead.Length);
            }
            starts.Add(0);
            (int At, int Len)? approximate = null;
            foreach (var at in starts)
            {
                if (at + needle.Length > folded.Length) continue;
                var span = folded.Substring(at, needle.Length);
                if (span == needle) return (at, needle.Length);
                if (approximate == null && needle.Length >= MIN_APPROXIMATE_LENGTH &&
                    Differing(span, needle) <= 1)
                {
                    approximate = (at, needle.Length);
                }
            }
            return approximate;
        }

        /// <summary>何文字が違うか——構成上、長さは等しい。</summary>
        private static int Differing(string a, string b)
        {
            var count = 0;
            for (var i = 0; i < a.Length; i++)
            {
                if (a[i] != b[i])
                {
                    count++;
                    if (count > 1) return count;
                }
            }
            return count;
        }

        /// <summary>
        /// 名前に続くものから用件を切り出す。
        ///
        /// 裁くのは**前だけ**：所有者が名前の周りに置いた句読点（「、」「。」）は用件の一部ではないが、
        /// **用件の終わりの句読点は用件である**——エージェントは一文を見るべきで、読み上げも自前の句点を欲しがる。
        /// </summary>
        private static string Clean(string rest)
        {
            var s = TrimStartPunct(rest);
            if (IsIgnorable(s)) return "";
            var head = Fold(s).Text;
            foreach (var vocative in VOCATIVE)
            {
                var suffix = Fold(vocative).Text;
                if (head.StartsWith(suffix, StringComparison.Ordinal))
                {
                    s = TrimStartPunct(s.Substring(suffix.Length));
                    break;
                }
            }
            return TrimEndWs(IsIgnorable(s) ? "" : s);
        }

        private static bool IsIgnorable(string s)
        {
            var folded = Fold(s).Text;
            if (folded.Length == 0) return true;
            foreach (var word in IGNORABLE)
            {
                if (Fold(word).Text == folded) return true;
            }
            return false;
        }

        /// <summary>一文字の比較可能な形：カタカナ・小文字・濁音なし、或は落とす。</summary>
        private static char? FoldChar(char ch)
        {
            var b = StripVoicing(ch);
            // ひらがな → カタカナ（`base + 0x60`）。
            if (b >= '\u3041' && b <= '\u3096') return (char)(b + 0x60);
            if ((b >= 'a' && b <= 'z') || (b >= 'A' && b <= 'Z')) return char.ToLowerInvariant(b);
            if (b >= '0' && b <= '9') return b;
            // ⚠️ Kotlin の `Char.isWhitespace()`（NEL／U+180E は空白でない）——`char.IsWhiteSpace` は使わない。
            if (KotlinText.IsWhitespace(b)) return null;
            if (PUNCT.IndexOf(b) >= 0) return null;
            // Kotlin の `Char.isLetterOrDigit()`。.NET の `char.IsLetterOrDigit` は Letter ∪ Nd ＝ 同じ。
            if (char.IsLetterOrDigit(b)) return b;
            return null;
        }

        /// <summary>
        /// 濁点を落とす：「ムツギ」と「ミラ」は聞き手には同じ名前であり、子音は合っていて
        /// 有声化だけ間違えた転写も、その名前を言ったことには変わりない。
        /// </summary>
        private static char StripVoicing(char ch) => VOICING.TryGetValue(ch, out var b) ? b : ch;

        private static Dictionary<char, char> BuildVoicing()
        {
            var rows = new List<KeyValuePair<string, string>>
            {
                new KeyValuePair<string, string>(
                    "かきくけこさしすせそたちつてとはひふへほ", "がぎぐげござじずぜぞだぢづでどばびぶべぼ"),
                new KeyValuePair<string, string>("はひふへほ", "ぱぴぷぺぽ"),
                new KeyValuePair<string, string>("う", "ゔ"),
            };
            var m = new Dictionary<char, char>(64);
            foreach (var row in rows)
            {
                var b = row.Key;
                var v = row.Value;
                for (var i = 0; i < b.Length; i++)
                {
                    m[v[i]] = b[i];
                    m[(char)(v[i] + 0x60)] = (char)(b[i] + 0x60);
                }
            }
            return m;
        }

        /// <summary>`rest.trimStart(*PUNCT)`——先頭の記号を落とす。</summary>
        private static string TrimStartPunct(string s)
        {
            var i = 0;
            while (i < s.Length && PUNCT.IndexOf(s[i]) >= 0) i++;
            return s.Substring(i);
        }

        /// <summary>`String.trimEnd()`——末尾の **Kotlin の空白**を落とす（<see cref="KotlinText.IsWhitespace"/>）。</summary>
        private static string TrimEndWs(string s)
        {
            var end = s.Length;
            while (end > 0 && KotlinText.IsWhitespace(s[end - 1])) end--;
            return s.Substring(0, end);
        }

        private static List<string> Combine(List<string> head, params string[] tail)
        {
            var outp = new List<string>(head);
            outp.AddRange(tail);
            return outp;
        }
    }
}
