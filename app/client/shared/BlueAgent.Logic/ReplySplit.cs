using System;
using System.Collections.Generic;
using System.Text;
using System.Text.RegularExpressions;

namespace BlueAgent.Logic
{
    /// <summary>
    /// 回覆的**文面分段**——C# 版（共用邏輯層的第一支，Unity 呈現層吃這一支）。
    ///
    /// 這一支是 `blueagent/live2d/android/kmp/src/commonMain/.../ReplySplit.kt` 的**逐行移植**；
    /// 等價性由 `fixtures/reply_split.json`（Kotlin 那一側機器產生的真值）逐條對拍，
    /// 不靠「看起來差不多」。
    ///
    /// owner 2026-10-01 定案（原句）：
    /// - 「長回覆最好按照文面拆分成多個回覆。間隔以語音長度做基準」
    /// - 「**每條最多 3 句話**，按照這個基準分」⇒ 條數隨長度長出來（30 句＝10 條），不是「最多 3 條」。
    ///
    /// 純文字的事（分段／估長度）留在這一層；顯示成幾個氣泡、什麼時候顯示，是呈現層的事。
    /// </summary>
    public static class ReplySplit
    {
        /// <summary>一條氣泡最多幾句（owner 定案）。</summary>
        public const int SentencesPerBubble = 3;

        /// <summary>句尾標點：切句用（中日文全角與英文半角都收，標點留在原句上）。</summary>
        private static readonly char[] SentenceEnd = { '。', '！', '？', '!', '?', '…', '．' };

        /// <summary>句尾還可能黏著的收尾符號：`「…。」` 這種要整句一起切。</summary>
        private static readonly char[] Trailing = { '」', '』', '）', ')', '】', '］', '"', '\'', '”', '’' };

        /// <summary>
        /// 表情包記號（`:name`）：**不念、也不算語音長度**。只認形狀（這一層沒有名字表）。
        /// 整條只有記號的氣泡必須算成 0 字——否則她念完之後畫面還要空等幾秒才跳出那張貼圖。
        /// </summary>
        private static readonly Regex Sticker = new Regex(":[a-z0-9_]{2,32}:?", RegexOptions.Compiled);

        // JVM 的 `\s` 只有 ASCII（見 KotlinText.JavaSpaceClass）——**不要**用 C# 的 `\s`（Unicode 級，
        // 會把 NBSP 之類的字元也吃進空行判定；fixture 的 `paragraph_break_with_unicode_space` 鎖住了）。
        private static readonly Regex BlankLine =
            new Regex("\\n" + KotlinText.JavaSpaceClass + "*\\n", RegexOptions.Compiled);

        /// <summary>
        /// 把一段回覆切成氣泡，**每一條最多 <paramref name="max"/> 句**。
        /// ①先看他自己的空行分段（作者分好的段不併掉）；②每段內部按句尾標點切句，每 <paramref name="max"/> 句一條。
        /// </summary>
        public static List<string> Groups(string text, int max = SentencesPerBubble)
        {
            var t = KotlinText.Trim(text);
            var output = new List<string>();
            if (t.Length == 0) return output;
            if (max <= 1) return SentencesOrSelf(t);
            var paragraphs = new List<string>();
            foreach (var raw in BlankLine.Split(t))
            {
                var one = KotlinText.Trim(raw);
                if (one.Length > 0) paragraphs.Add(one);
            }
            if (paragraphs.Count == 0) return new List<string> { t };
            foreach (var paragraph in paragraphs)
            {
                var lines = Sentences(paragraph);
                if (lines.Count == 0)
                {
                    output.Add(paragraph);
                    continue;
                }
                for (var i = 0; i < lines.Count; i += max)
                {
                    var sb = new StringBuilder();
                    for (var k = i; k < Math.Min(i + max, lines.Count); k++) sb.Append(lines[k]);
                    output.Add(sb.ToString());
                }
            }
            output.RemoveAll(string.IsNullOrWhiteSpace);
            return output;
        }

        /// <summary>沒標點可切時的退路：整段一條（避免空清單讓畫面什麼都沒有）。</summary>
        private static List<string> SentencesOrSelf(string text)
        {
            var lines = Sentences(text);
            return lines.Count > 0 ? lines : new List<string> { text };
        }

        /// <summary>每段結束時的累計「語音長度」估算（毫秒）：字數 × <paramref name="msPerChar"/>。</summary>
        public static List<long> BoundsMs(IEnumerable<string> groups, int msPerChar)
        {
            var output = new List<long>();
            long sum = 0;
            foreach (var g in groups)
            {
                sum += Chars(g) * (long)msPerChar;
                output.Add(sum);
            }
            return output;
        }

        /// <summary>一段文字「幾個字」：不含空白（空白不出聲），也不含貼圖記號（貼圖不出聲）。</summary>
        public static int Chars(string text)
        {
            var stripped = Sticker.Replace(text ?? string.Empty, string.Empty);
            var count = 0;
            foreach (var c in stripped)
            {
                if (!KotlinText.IsWhitespace(c)) count++;
            }
            return count;
        }

        /// <summary>這段文字**念得出來**嗎：只有貼圖記號 ⇒ 沒有東西可念（不該拿去合成）。</summary>
        public static bool Speakable(string text) => Chars(text) > 0;

        /// <summary>按句尾標點切句（標點留在句尾；連續的標點與其後的收尾引號黏在同一句；換行也當句尾）。</summary>
        public static List<string> Sentences(string text)
        {
            var output = new List<string>();
            if (string.IsNullOrEmpty(text)) return output;
            var sb = new StringBuilder();
            var i = 0;
            while (i < text.Length)
            {
                var c = text[i];
                sb.Append(c);
                i++;
                if (c == '\n')
                {
                    var one = KotlinText.Trim(sb.ToString());
                    if (one.Length > 0) output.Add(one);
                    sb.Clear();
                    continue;
                }
                if (Array.IndexOf(SentenceEnd, c) >= 0)
                {
                    while (i < text.Length &&
                           (Array.IndexOf(SentenceEnd, text[i]) >= 0 || Array.IndexOf(Trailing, text[i]) >= 0))
                    {
                        sb.Append(text[i]);
                        i++;
                    }
                    var one = KotlinText.Trim(sb.ToString());
                    if (one.Length > 0) output.Add(one);
                    sb.Clear();
                }
            }
            var rest = KotlinText.Trim(sb.ToString());
            if (rest.Length > 0) output.Add(rest);
            output.RemoveAll(x => x.Length == 0);
            return output;
        }
    }
}
