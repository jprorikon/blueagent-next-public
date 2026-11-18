using System;
using System.Collections.Generic;
using System.Text;

namespace BlueAgent.Logic
{
    /// <summary>
    /// 亂數來源的接縫——對應 Kotlin 的 `kotlin.random.Random`（G8 也看得懂：就這一個方法）。
    ///
    /// 為什麼要有接縫：session id 是**亂數**的產物，要能機械對拍就得把亂數換成腳本
    /// （測試端給一支照腳本交值的實作），見 `BlueAgent.Logic.Tests/SessionIdsFixtureTest.cs`。
    /// </summary>
    public interface IRandomSource
    {
        /// <summary>
        /// 回傳 [0, <paramref name="bound"/>) 的整數；<paramref name="bound"/> ≤ 0 要丟例外
        /// （照 Kotlin `Random.nextInt(bound)` 的規定）。
        /// </summary>
        int NextInt(int bound);
    }

    /// <summary>
    /// 橋線的 session id——C# 版（`blueagent/live2d/android/kmp/src/commonMain/.../SessionIds.kt`
    /// 的**逐行移植**）。等價性由 `fixtures/session_ids.json`、`fixtures/session_ids_reroll.json`
    /// （Kotlin 那一側機器產生的真值）逐條對拍，不靠「看起來差不多」。
    ///
    /// 為什麼要有輪替：歷史面板是按每一列的 `session` 分組的，而手機每次送出的請求都帶同一個字串
    /// ⇒ 面板永遠只有一組。按「開始新對話」＝換一個新的 id。（規則與界面無關，所以放在共用層。）
    ///
    /// ## 2026-10-03 第三版：**純 UUID，不帶前綴**
    ///
    /// owner 的原話：「Session ID 最好不要前綴，直接用 UUID 啥的」。前兩版是 `&lt;名字&gt;-&lt;6 位&gt;`
    /// （`blueagent-phone-k7m2qp`；iOS 那側還自己手抄一份 `blueagent-ios-xxxxxx`）。前綴有兩個毛病：
    /// ①**會說謊**（兩台都是手機，一台叫 phone、一台叫 ios）；②出貨預設值是**所有機器同一個**字串，而橋側
    /// 的訂閱按 session 分桶 ⇒ 兩台同時首次啟動會**共用同一個桶**（她回給哪一台、判人事件送去哪一台都會亂
    /// ——owner 同一天抓到的「直接撞機器」）。現在：
    /// <list type="bullet">
    /// <item><see cref="Roll(string, IRandomSource)"/> ⇒ `xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx`
    /// （UUID v4 形狀；Kotlin 那側用 `kotlin.random` 產生——`commonMain` 沒有 `java.util.UUID`，
    /// 也不吃 `kotlin.uuid` 的實驗標記。C# 這側同理：自己組，不靠任何 UUID 套件，
    /// 免得「兩邊的 UUID 產生器幾版之後行為不同」。）</item>
    /// <item><see cref="EnsureUnique(string, IRandomSource)"/> ⇒ 只有「空白／出貨預設名／只剩舊前綴沒有尾巴」
    /// 這三種**會撞的**才換成 UUID；已經唯一的（舊版的 `blueagent-phone-e538bj`、他自己取的名字）
    /// **原樣不動**——重 roll 只會把他那台裝置在橋側的會話切掉。前綴只是不好看、不會撞，
    /// 下次他自己按「開始新對話」就變 UUID。</item>
    /// </list>
    /// </summary>
    public static class SessionIds
    {
        /// <summary>出貨預設值：只當「還沒定過」的標記，**不會被送出去**（開機 <see cref="EnsureUnique(string)"/> 換成 UUID）。</summary>
        public const string Default = "blueagent-phone";

        /// <summary>舊版留下的前綴（「只有前綴、沒有尾巴」＝會跟別台撞名的那一種）。</summary>
        private static readonly HashSet<string> LegacyPrefixes =
            new HashSet<string>(StringComparer.Ordinal) { "blueagent-phone", "blueagent-ios" };

        /// <summary>UUID 的字串長度（8-4-4-4-12 ＋ 4 個連字號）。</summary>
        public const int UuidLen = 36;

        /// <summary>十六進位的字元表（小寫；長度 16 就是取值的 bound）。</summary>
        private const string Hex = "0123456789abcdef";

        /// <summary>variant 位可用的字元（長度 4 就是取值的 bound）。</summary>
        private const string Variants = "89ab";

        /// <summary>
        /// 下一個 session id：**純 UUID v4 形狀**（用系統亂數）。
        ///
        /// [current] 只拿來保證「不會 roll 回同一個」——真撞上時他會以為按鈕沒反應
        /// （那正是這個功能被回報的原因）。
        /// </summary>
        public static string Roll(string current) => Roll(current, new SystemRandomSource());

        /// <summary>
        /// 同上，可注入 <paramref name="random"/>（單測固定序列）。
        ///
        /// Kotlin 那份刻意**不寫預設參數**（預設參數不會過 framework 頭檔，iOS 那側就編不出只給
        /// `current` 的呼叫）；C# 沒有那個毛病，所以兩個 overload 都給——但**行為必須一樣**，
        /// 別在 overload 裡加料。
        /// </summary>
        public static string Roll(string current, IRandomSource random)
        {
            var now = KotlinText.Trim(current);
            while (true)
            {
                var candidate = Uuid4(random);
                if (candidate != now) return candidate;
            }
        }

        /// <summary>
        /// 裝置**自己該用的** id：空白、還停在出貨預設名、或只剩一個舊前綴（沒有尾巴）⇒ 換一個 UUID；
        /// 已經唯一的 ⇒ 原樣（**連 trim 後的值**——`"  他的手機  "` ⇒ `"他的手機"`）。
        ///
        /// 為什麼只換這三種（owner 2026-10-03）：橋側的訂閱是按 session 分桶的
        /// （`SUBS.setdefault(session, …)`）⇒ 兩台機器同時用預設名會**共用同一個桶**：她的回覆／判人事件／
        /// 預開閘狀態互相送到對方那台去（＝「直接撞機器」）。反之，已經唯一的 id 重 roll 只會把那一台在
        /// 橋側的會話切掉，一點好處都沒有。
        /// </summary>
        public static string EnsureUnique(string current) => EnsureUnique(current, new SystemRandomSource());

        /// <summary>同上，可注入 <paramref name="random"/>（單測固定序列）。</summary>
        public static string EnsureUnique(string current, IRandomSource random)
        {
            var now = KotlinText.Trim(current);
            if (now.Length == 0 || now == Default || LegacyPrefixes.Contains(now)) return Roll(now, random);
            return now;
        }

        /// <summary>
        /// UUID v4 的形狀：版本位固定 `4`、variant 位取 `89ab`（照慣例，人眼一看就知道是 v4）。
        ///
        /// ⚠️ **消耗順序必須與 Kotlin 完全一致**：8 → 4 → 3 → **1（bound=4）** → 3 → 12。
        /// 順序錯了（例如先把 variant 取出來）UUID 就對不上，而且對拍會看不出原因
        /// （兩邊都長得像合法 UUID）。所以這裡**照字串模板的求值順序逐步 append**，
        /// 不要重排、不要抽成一句 LINQ。
        /// </summary>
        private static string Uuid4(IRandomSource random)
        {
            var sb = new StringBuilder(UuidLen);
            AppendHex(sb, 8, random);
            sb.Append('-');
            AppendHex(sb, 4, random);
            sb.Append("-4");
            AppendHex(sb, 3, random);
            sb.Append('-');
            sb.Append(Variants[random.NextInt(Variants.Length)]);
            AppendHex(sb, 3, random);
            sb.Append('-');
            AppendHex(sb, 12, random);
            return sb.ToString();
        }

        /// <summary>Kotlin `hex(n)`：連續取 <paramref name="count"/> 個十六進位字元（每個字元消耗一次亂數）。</summary>
        private static void AppendHex(StringBuilder sb, int count, IRandomSource random)
        {
            for (var i = 0; i < count; i++) sb.Append(Hex[random.NextInt(Hex.Length)]);
        }


        /// <summary>
        /// <see cref="IRandomSource"/> 的系統實作：包 `System.Random`（對應 Kotlin 的 `Random.Default`）。
        /// </summary>
        private sealed class SystemRandomSource : IRandomSource
        {
            private readonly Random _random = new Random();

            public int NextInt(int bound)
            {
                // Kotlin 的 nextInt(bound) 要求 bound > 0，C# 的 Random.Next(bound) 也一樣會丟例外。
                if (bound <= 0) throw new ArgumentOutOfRangeException(nameof(bound), bound, "bound 必須 > 0");
                return _random.Next(bound);
            }
        }
    }
}
