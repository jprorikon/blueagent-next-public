using System;

namespace BlueAgent.Logic
{
    /// <summary>
    /// **Kotlin／JVM 的文字語意**——集中放「C# 與 JVM 天生不同」的那幾處，全共用邏輯層只有這一來源。
    ///
    /// 為什麼要單獨一支：這些差異**安靜**（編譯得過、看起來一樣、只在罕見碼位出錯），而且會散到每一支
    /// 用到文字的地方。fixture 已經抓到兩次：
    ///   * `ReplySplit` 的 `unicode_ws_trim`——U+180E 在 Kotlin 側**不是**空白（要留在句子裡），
    ///     C# 的 `char.IsWhiteSpace` 卻把它當空白 ⇒ 少一個字元；
    ///   * `SessionIds` 的 `ensure_unique_nel_only`——U+0085（NEL）在 Kotlin 側不是空白，
    ///     名字會原樣留下；C# 的 `Trim()` 會裁掉 ⇒ 變成「空名字」而被換成新 UUID。
    ///
    /// 判準是「逐碼位等價」，所以照 JVM 的定義自己寫死，**不依賴 .NET 的 Unicode 版本差異**
    /// （那正是上面兩個 bug 的來源）。
    /// </summary>
    public static class KotlinText
    {
        /// <summary>
        /// `Char.isWhitespace()` ＝ `Character.isWhitespace(c) || Character.isSpaceChar(c)`，
        /// 也就是：所有 Zs／Zl／Zp 分隔字元（**含** U+00A0 NBSP、U+2007、U+202F、U+3000）
        /// ＋ `\t \n \u000B \f \r` ＋ U+001C–U+001F。
        /// **不含** U+0085（NEL）與 U+180E——這兩個 `char.IsWhiteSpace` 會回 true，所以不能用它。
        /// </summary>
        public static bool IsWhitespace(char c)
        {
            switch (c)
            {
                case '\t': case '\n': case '\u000B': case '\f': case '\r': return true;
                case '\u001C': case '\u001D': case '\u001E': case '\u001F': return true;
                case '\u0020': case '\u00A0': case '\u1680': return true;
                case '\u2028': case '\u2029': case '\u202F': case '\u205F': case '\u3000': return true;
            }
            return c >= '\u2000' && c <= '\u200A';   // 其餘 Zs
        }

        /// <summary>`String.trim()`：裁掉頭尾的 <see cref="IsWhitespace"/>（null 當空字串）。</summary>
        public static string Trim(string s)
        {
            if (string.IsNullOrEmpty(s)) return string.Empty;
            var start = 0;
            var end = s.Length;
            while (start < end && IsWhitespace(s[start])) start++;
            while (end > start && IsWhitespace(s[end - 1])) end--;
            return s.Substring(start, end - start);
        }

        /// <summary>
        /// **JVM regex 的 `\s`（只有 ASCII）**：`[ \t\n\x0B\f\r]`。
        /// C# 的 `\s` 是 Unicode 級——拿它寫 `\n\s*\n` 會多吃 NBSP 之類的字元，空行判定就跟著走鐘
        /// （所以組 regex 時要接這條常數，不要用 `\s`）。
        /// </summary>
        public const string JavaSpaceClass = "[ \\t\\n\\u000B\\f\\r]";
    }
}
