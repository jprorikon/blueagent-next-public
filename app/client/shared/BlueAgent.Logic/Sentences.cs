using System.Collections.Generic;
using System.Text;

namespace BlueAgent.Logic
{
    /// <summary>
    /// 把一段回覆切成「念得出來的小塊」——句級串流管線的兩半。
    ///
    /// **為什麼存在**：GPT-SoVITS 合成一句話的時間大約就是那句話被念出來的時間，所以先把整段回覆
    /// 合成完才開始播，等於要等整個答案跑完才聽得到第一個字。在本機實測：一段 16 秒的回覆花了
    /// 15.8 秒合成，這段時間使用者什麼都沒聽到。
    ///
    /// 有兩種用法，差別就是那 2–4 秒的沉默：
    ///
    ///  * [Split] 作用在**已經完成**的回覆上——gateway 路徑，一次把整段文字交出來；
    ///  * [Stream] 則是**吃 bridge 進來的 delta**。一句話在串流裡一完成，就可以送進合成器，
    ///    第一個聲音就不用等整段答案。這件事之所以安全，正是因為 token 串流是**只能追加**的：
    ///    一句話一旦被它的句尾字元收掉，後面的 delta 只可能在它**之後**加字，不可能改寫它。
    ///    （句尾之後唯一還可能到的是收尾引號——`「おはよう。」`——這也是為什麼一句話正好結束在
    ///    緩衝尾端時，要多等一個 delta。）
    ///
    /// 切法刻意保守：
    ///   * 在日文／ASCII 句尾字元後切，句尾字元留著（還把屬於那一句的收尾引號／括號一起帶走：
    ///     `「おはよう。」` 保持完整）；
    ///   * 換行也是句尾；
    ///   * **任何一塊都不超過 [DefaultMaxChars]**——一整面沒有標點的字牆也必須能串流，所以退回
    ///     最近的自然停頓（、 ， , 空白），再沒有就硬切；
    ///   * 在語音管線（[SplitForSpeech] / [SpeechStream]）裡，自然停頓也會提早收掉一塊，只要那一塊
    ///     已經長到足以涵蓋下一塊的合成時間（[SpeechSoftMinChars]）——那個數字背後的量測見該常數。
    ///
    /// [Stream] 寫成**產出的東西與 [Split] 完全相同**，測試用「同一段文字的所有可能切塊」餵進去證明
    /// 這點。兩支切法如果走鐘，就表示同一則回覆會因為走哪條路徑而聽起來不一樣。
    ///
    /// 純函式、全函式：不碰 Android、不丟例外，所以有趣的情形都進得了單元測試。
    ///
    /// 這一支是 `blueagent/live2d/android/kmp/src/commonMain/.../Sentences.kt` 的**逐行移植**；
    /// 等價性由 `fixtures/sentences.json`（Kotlin 那一側機器產生的真值）逐條對拍，
    /// 不靠「看起來差不多」。
    /// </summary>
    public static class Sentences
    {
        /// <summary>句尾字元。`；`／`;` 也算：它們念出來就是一個句號。</summary>
        private const string ENDERS = "。！？!?；;";

        /// <summary>收尾引號／括號：屬於剛結束的那一句。</summary>
        private const string TRAILERS = "」』）)】］》〉”\"'";

        /// <summary>一塊超過長度上限時用的軟停頓。</summary>
        private const string SOFT = "、，, ";

        /// <summary>單獨一個標點不值得念。</summary>
        private const string NOISE = ENDERS + TRAILERS + SOFT;

        public const int DefaultMaxChars = 60;

        /// <summary>
        /// 語音管線的 [SOFT] 停頓規則從幾個字開始生效（單位：字元）。
        ///
        /// 硬上限（[DefaultMaxChars]）回答的是另一個問題——「這樣一次丟給合成器太長了」——60 就是它
        /// 以前會咬人的地方。但決定「她會不會在一句長句中間突然安靜」的是另一個數字：前一塊的
        /// **播放**時間是否涵蓋這一塊的**合成**時間。在本機實測（2026-09-30
        /// `blueagent/kws/TTS-HOST-AND-LATENCY-20260930.md`）服務大約每字 0.08 秒、每次請求再加約
        /// 0.4 秒，而它產出的音訊大約每字 0.2 秒——所以一塊約 24 字以上的話，被念出來的時間大約是
        /// 做它的兩倍，而 53 字的句子就不是（合成 4.6 秒 vs 前一句只播了約 3 秒 ⇒ 中間一段沉默，
        /// 也就是 owner 2026-09-30 抱怨的那個空隙）。
        ///
        /// 取 24 而不是更小：每一塊都要付那固定約 0.4 秒的來回，這在 24 字那一塊賺到的約 2 秒裡
        /// 還算合理的一塊，但幾乎就是 8 字那一塊能賺回來的全部。
        /// </summary>
        public const int SpeechSoftMinChars = 24;

        /// <summary>
        /// 把一段回覆切成可念的小塊。預設 [softMin] = int.MaxValue ⇒ 關閉軟停規則。
        /// 等價於 `Stream(maxChars, softMin).feed(text) + flush()`。
        /// </summary>
        public static List<string> Split(string text, int maxChars = DefaultMaxChars, int softMin = int.MaxValue)
        {
            var stream = new Stream(maxChars, softMin);
            var output = stream.Feed(text);
            output.AddRange(stream.Flush());
            return output;
        }

        /// <summary>[Split] 套上 shell 語音管線用的規則——見 [SpeechSoftMinChars]。</summary>
        public static List<string> SplitForSpeech(string text) =>
            Split(text, DefaultMaxChars, SpeechSoftMinChars);

        /// <summary>給語音管線用的 [Stream]——與 [SplitForSpeech] 同一套規則。</summary>
        public static Stream SpeechStream() => new Stream(DefaultMaxChars, SpeechSoftMinChars);

        /// <summary>這一塊值得一趟合成來回嗎（不是只有零星的標點）。</summary>
        private static bool Speakable(string piece)
        {
            if (piece.Length == 0) return false;
            foreach (var c in piece)
            {
                if (NOISE.IndexOf(c) < 0) return true;
            }
            return false;
        }

        /// <summary>`piece` 裡最後一個自然停頓的後一位索引；沒有就 -1。</summary>
        private static int LastSoftBreak(string piece)
        {
            for (var i = piece.Length - 1; i >= 0; i--)
            {
                if (SOFT.IndexOf(piece[i]) >= 0) return i + 1;
            }
            return -1;
        }

        // 裁頭尾空白與空白判定都走 <see cref="KotlinText"/>——**與 JVM 逐碼位等價的那一份**。
        // （NBSP／U+2007／U+202F／U+3000 要裁；U+0085（NEL）、U+180E **不裁**——
        //  C# 的 `char.IsWhiteSpace`／`string.Trim()` 剛好相反，fixture 已抓過兩次。）
        /// <summary>
        /// 增量切分器：規則與 [Split] 相同，但可以邊串流邊餵。
        ///
        /// 本身**不是**執行緒安全的——shell 從 bridge 的讀取執行緒餵它，也從回合收尾餵它，
        /// 並把這些都留在同一條執行緒上。
        /// </summary>
        public sealed class Stream
        {
            private readonly int _maxChars;
            private readonly int _softMin;
            private readonly StringBuilder _buf = new StringBuilder();

            public Stream(int maxChars = DefaultMaxChars, int softMin = int.MaxValue)
            {
                _maxChars = maxChars;
                _softMin = softMin;
            }

            /// <summary>吸收另一個 delta；回傳現在已經完整（通常沒有、或一個）的句子。</summary>
            public List<string> Feed(string delta)
            {
                if (delta == null || delta.Length == 0) return new List<string>();
                _buf.Append(delta);
                return Drain(atEnd: false);
            }

            /// <summary>不會再有文字了：把最後一句吐出來，就算沒有句尾字元也一樣。</summary>
            public List<string> Flush() => Drain(atEnd: true);

            private List<string> Drain(bool atEnd)
            {
                var output = new List<string>();
                while (true)
                {
                    var cut = CutPoint(atEnd);
                    if (!cut.HasValue) break;
                    var piece = KotlinText.Trim(_buf.ToString(0, cut.Value));
                    // 吃掉已經取走的那一段。**不能用 `buf.delete(0, cut)`** —— 那是 JVM 的
                    // java.lang.StringBuilder 才有的方法，common 的 StringBuilder 沒有；在 JVM target
                    // 上編得過、一編 iOS（Kotlin/Native）就 `Unresolved reference 'delete'`
                    // （2026-10-02 在 Mac 上第一次編 framework 時抓到：這個模組先前從沒編過 iOS，
                    //  「commonMain」其實一直是「JVM 專用」）。
                    var rest = _buf.ToString(cut.Value, _buf.Length - cut.Value);
                    _buf.Length = 0;
                    _buf.Append(rest);
                    if (Speakable(piece)) output.Add(piece);
                }
                if (atEnd)
                {
                    var rest = KotlinText.Trim(_buf.ToString());
                    _buf.Length = 0;
                    if (Speakable(rest)) output.Add(rest);
                }
                return output;
            }

            /// <summary>
            /// 下一塊完整內容結束在哪，或是在還需要更多文字時回 null。
            ///
            /// 掃描是**照文字順序**的，這件事要緊：一塊非常長的內容雖然後面某處有句尾字元，也必須在
            /// 上限處切；而句尾字元與上限撞在同一個字元上時句尾字元必須勝出——兩者都是為了與
            /// [Split] 完全一致。
            /// </summary>
            private int? CutPoint(bool atEnd)
            {
                var i = 0;
                var n = _buf.Length;
                while (i < n)
                {
                    var ch = _buf[i];
                    if (ch == '\n' || ch == '\r') return i + 1;
                    if (ENDERS.IndexOf(ch) >= 0)
                    {
                        var j = i + 1;
                        while (j < n && TRAILERS.IndexOf(_buf[j]) >= 0) j++;
                        if (j < n || atEnd) return j;
                        // 句尾字元是我們持有的最後一個字元。收尾引號可能還在路上，在這裡切會把
                        // `「おはよう。」` 拆成兩半。等待要付一個 delta（約 50 ms）；不等的代價是
                        // 一句錯的話被念出來。
                        return null;
                    }
                    // 語音管線的規則：一旦這一塊已經長到它自己的播放時間可以涵蓋下一塊的合成
                    // （[softMin]，見 [SpeechSoftMinChars]），下一個自然停頓就把它收掉。預設關閉
                    // （`softMin == int.MaxValue`），所以 [Split] 與 [Stream] 對其他呼叫者逐位元相同。
                    if (_softMin != int.MaxValue && SOFT.IndexOf(ch) >= 0 && i + 1 >= _softMin) return i + 1;
                    if (i >= _maxChars)
                    {
                        var soft = LastSoftBreak(_buf.ToString(0, i));
                        return soft > 0 ? soft : i;
                    }
                    i++;
                }
                return null;
            }
        }
    }
}
