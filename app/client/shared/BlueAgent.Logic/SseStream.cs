using System;
using System.Collections.Generic;
using System.Text;

namespace BlueAgent.Logic
{
    /// <summary>
    /// **SSE 的切分器**：網路上過來的位元組 → 一幀一幀的 `(event, data)`。
    ///
    /// 為什麼放在共用邏輯層：Kotlin 那一側這段是寫在 `StreamBridgeClient` 的迴圈裡（五條規則），
    /// 而**每一端的傳輸都會把位元組切成不一樣的塊**（OkHttp 一條一行地讀、Unity 一次收一塊、
    /// iOS 用 `URLSession` 又一塊）——切塊方式不同、規則必須一樣，否則「同一條橋、兩端看到不同幀」。
    /// 這裡是那份規則的**唯一實作**，兩端（Unity 的 `BridgeClient`、測試）都吃它。
    ///
    /// 對照 Kotlin（`StreamBridgeClient.onResponse` 的內圈，逐條照抄）：
    /// <code>
    /// var event = ""
    /// while (!source.exhausted()) {
    ///     val line = source.readUtf8Line() ?: break
    ///     when {
    ///         line.startsWith("event:") -> event = line.substring(6).trim()
    ///         line.startsWith("data:")  -> BridgeProtocol.parseEvent(event, line.substring(5))?.let { … }
    ///     }
    /// }
    /// </code>
    /// 三件**刻意照抄、不可以「改善」**的事：
    /// <list type="number">
    /// <item><b>`event` 名字跨空行保留</b>：橋的寫法是 `event: X\ndata: {…}\n\n`（見 `bridge_voice.sse()`），
    ///   但**判斷只看行首**——空行、`:` 註解行、`id:` 行都不會把名字清掉。寫成「空行＝一幀結束」的
    ///   標準 SSE 讀法，會在多行 `data:` 的串流上與 Kotlin 走鐘。</item>
    /// <item><b>每個 `data:` 行各自成一幀</b>（不是把多行黏起來）：標準 SSE 會把同一個 event 下的多個
    ///   `data:` 用 `\n` 接起來再解 —— Kotlin 那側是**一行一解**。</item>
    /// <item><b>`data:` 的值原樣帶出、不 trim</b>（`substring(5)`）；裁切是 `ParseEvent` 進去做的事。
    ///   ⚠️ 橋的組幀是 `f"data: {json}"` ⇒ **冒號後面有一個空白**，所以切出來的 `Data` 開頭是
    ///   `" "`（實測：`" {\"src\":\"type\"}"`）。標準 SSE 會把冒號後那一個空白吃掉，Kotlin **不會**
    ///   ⇒ 這裡也不吃（吃了就與手機看到的不一樣）；那個空白最後由 `ParseEvent` 的 trim 消化掉。
    ///   `event:` 的名字**要** trim（`substring(6).trim()`，且是 Kotlin 的 trim 語意 ⇒ 走
    ///   <see cref="KotlinText"/>，NBSP 那幾顆要裁）。</item>
    /// </list>
    /// 換行語意：一行＝到下一個 `\n` 為止，**行尾的 `\r` 拿掉**（OkHttp 的 `readUtf8Line`），
    /// 最後一行沒有換行也算一行（`Flush()`）。
    ///
    /// ⚠️ **位元組層的多位元組字元**：一塊的尾端可能正好切在一個日文字元的 3 個位元組中間
    /// （線上實測：615 KB 的那串被切成 157 塊）。用 <see cref="Decoder"/> 保住半個字元，
    /// **不要**自己 `Encoding.UTF8.GetString(chunk)`（會把那半個字變成 U+FFFD）。
    ///
    /// 這一支**不碰網路**（純文字），所以能在 NAS 上離線單測；連線、逾時、重試留在各端。
    /// </summary>
    public sealed class SseStream
    {
        /// <summary>一幀的兩個欄位：`event:` 的名字（已 trim）與 `data:` 的內容（原樣）。</summary>
        public readonly struct Wire
        {
            public Wire(string @event, string data) : this(@event, data, "") { }

            public Wire(string @event, string data, string id)
            {
                Event = @event;
                Data = data;
                Id = id;
            }

            /// <summary>`id:` 那一行的值（重連補洞用 `Last-Event-ID`）；沒有 ⇒ 空字串。</summary>
            public string Id { get; }

            /// <summary>`event:` 那一行的名字（Kotlin trim 過）；沒有 `event:` 行 ⇒ 空字串。</summary>
            public string Event { get; }

            /// <summary>`data:` 那一行的內容（**原樣**，沒 trim）。</summary>
            public string Data { get; }

            public override string ToString() => Event + " | " + Data;
        }

        private readonly Decoder _decoder = new UTF8Encoding(false).GetDecoder();
        private readonly StringBuilder _partial = new StringBuilder();
        private string _event = "";
        //: `id:` 的值（契約 §v2：重連帶 `Last-Event-ID` 補斷線期間漏的訊息）
        private string _id = "";

        /// <summary>目前記著的 `event:` 名字（診斷用）。</summary>
        public string CurrentEvent => _event;

        /// <summary>還沒湊成一行、留在緩衝裡的位元組解出來的字（診斷用）。</summary>
        public int PartialLength => _partial.Length;

        /// <summary>收一塊位元組，回這塊裡湊出來的幀（可能 0 幀、也可能好幾幀）。</summary>
        public List<Wire> Push(byte[] buffer) => Push(buffer, 0, buffer == null ? 0 : buffer.Length);

        /// <summary>收一塊位元組（`offset`／`count` 給的是這塊的有效範圍）。</summary>
        public List<Wire> Push(byte[] buffer, int offset, int count)
        {
            var outList = new List<Wire>();
            if (buffer == null || count <= 0) return outList;

            var chars = new char[Encoding.UTF8.GetMaxCharCount(count)];
            _decoder.Convert(buffer, offset, count, chars, 0, chars.Length,
                             flush: false, out _, out var charsUsed, out _);
            Consume(chars, charsUsed, outList);
            return outList;
        }

        /// <summary>文字進來（測試／整段）：與 <see cref="Push(byte[])"/> 同一條路（自己編成位元組）。</summary>
        public List<Wire> PushText(string text)
        {
            if (string.IsNullOrEmpty(text)) return new List<Wire>();
            var bytes = Encoding.UTF8.GetBytes(text);
            return Push(bytes, 0, bytes.Length);
        }

        /// <summary>
        /// 串流結束（EOF）：把最後一行沒有換行的內容當一行處理（OkHttp 的 `readUtf8Line` 在 EOF
        /// 回剩下的內容、不是 null），並把卡在半個字元的解碼器收乾淨。
        /// 可以重複呼叫（第二次回空清單）。
        /// </summary>
        public List<Wire> Flush()
        {
            var outList = new List<Wire>();
            var chars = new char[8];
            _decoder.Convert(Array.Empty<byte>(), 0, 0, chars, 0, chars.Length,
                             flush: true, out _, out var charsUsed, out _);
            if (charsUsed > 0) Consume(chars, charsUsed, outList);
            if (_partial.Length > 0)
            {
                var line = _partial.ToString();
                _partial.Clear();
                ProcessLine(StripCr(line), outList);
            }
            return outList;
        }

        private void Consume(char[] chars, int count, List<Wire> outList)
        {
            for (var i = 0; i < count; i++)
            {
                var c = chars[i];
                if (c != '\n')
                {
                    _partial.Append(c);
                    continue;
                }
                var line = _partial.ToString();
                _partial.Clear();
                ProcessLine(StripCr(line), outList);
            }
        }

        /// <summary>行尾的 `\r` 拿掉（`\r\n` 與單獨的 `\n` 兩種都吃）。</summary>
        private static string StripCr(string line) =>
            line.Length > 0 && line[line.Length - 1] == '\r' ? line.Substring(0, line.Length - 1) : line;

        private void ProcessLine(string line, List<Wire> outList)
        {
            if (line.StartsWith("event:", StringComparison.Ordinal))
            {
                _event = KotlinText.Trim(line.Substring(6));
                return;
            }
            if (line.StartsWith("id:", StringComparison.Ordinal))
            {
                _id = KotlinText.Trim(line.Substring(3));
                return;
            }
            if (line.StartsWith("data:", StringComparison.Ordinal))
            {
                // ⚠️ 值不 trim（Kotlin 是 `substring(5)`）；空白的處理在 ParseEvent 裡。
                outList.Add(new Wire(_event, line.Substring(5), _id));
            }
            // 其餘（空行／`:` 註解／`id:`／`retry:`）一律不動 event 名字。
        }

        /// <summary>
        /// 橋那一側的組幀法（`docker/blueagent-server/bridge_voice.py` 的 `sse()`）：
        /// `event: {名}\ndata: {json}\n\n`（UTF-8）。測試用它造位元組，**不自己發明格式**。
        /// ⚠️ 冒號後面那個空白是**原樣的**（切出來的 `Data` 會以 `" "` 開頭，見類別註解）。
        /// </summary>
        public static byte[] FrameBytes(string @event, string data) =>
            Encoding.UTF8.GetBytes("event: " + @event + "\ndata: " + data + "\n\n");
    }
}
