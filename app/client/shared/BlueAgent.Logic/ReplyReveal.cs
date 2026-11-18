using System;
using System.Collections.Generic;

namespace BlueAgent.Logic
{
    /// <summary>
    /// 回覆分段的**顯示進度**——一個回覆要拆成幾個氣泡、現在已經顯示到第幾個。
    ///
    /// 這一支是 `blueagent/live2d/android/kmp/src/commonMain/.../ReplyReveal.kt` 的**逐行移植**；
    /// 等價性由 `fixtures/reply_reveal.json`（Kotlin 那一側機器產生的真值）逐條對拍，
    /// 不靠「看起來差不多」。
    ///
    /// owner 2026-10-01 定案：「長回覆最好按照文面拆分成多個回覆。間隔以語音長度做基準」、
    /// 「**每條最多 3 句話**」（＝<paramref name="maxSentences"/>，**每一條的上限**；條數隨長度長出來，
    /// 不是最多 3 條）。分幾條是 <see cref="ReplySplit"/> 的事（純函式）；這裡只管**什麼時候**顯示下一條。
    ///
    /// ⚠️ 這裡記著一個真實的錯（2026-10-01 owner 回報「消息分條刷新不正常：先一次性收到兩條，然後會清空
    /// 最新消息，然後按讀的順序顯示兩條」）：
    /// 這段狀態以前長在 `ShellController.setReply()` 裡，而 `setReply` 會在**串流中每個 delta** 被呼叫，
    /// 每次都被重設成「只顯示第一段」。於是文字越寫越多、畫面卻往回退：氣泡 3→2→1 清空重畫，
    /// 再被節拍器一條條爬回來。實測軌跡（`kws/exp/valve/trace_before.txt`）：
    /// `n=1→2→3`（13:32:52/:03，她一邊說一邊分段）→ `n=3→1`（13:33:13，定稿的 `onDone` 又呼叫了一次
    /// `setReply`）→ 再爬回 2、3。
    ///
    /// 所以這裡的不變式只有一條：**顯示過的段落只增不減**。文字變長只會讓「最後那一條」繼續長大
    /// （而且只允許「變長」——重新分段若把句子併走，也不會把已經顯示的字改掉），不會讓畫面上的東西
    /// 消失或重排。
    /// </summary>
    public sealed class ReplyReveal
    {
        /// <summary>每一條最多幾句（每一條的上限；條數隨長度長出來）。</summary>
        private readonly int _maxSentences;

        /// <summary>估語音長度用的每字毫秒。</summary>
        private readonly int _msPerChar;

        /// <summary>沒有語音幀時，節拍器每次假裝走多久。</summary>
        private readonly long _tickMs;

        /// <summary>已經顯示出去的氣泡（在顯示的那一刻定稿）。</summary>
        private readonly List<string> _shown = new List<string>();

        /// <summary>這一輪文字目前的切法（<see cref="ReplySplit.Groups"/>，每條最多 <see cref="_maxSentences"/> 句）。</summary>
        private List<string> _groups = new List<string>();

        /// <summary>每一段結束時的累計語音長度（毫秒）。</summary>
        private List<long> _bounds = new List<long>();

        /// <summary>這一輪「已經有聲音的長度」：有語音幀時是實測的 `seconds`，沒有時由 <see cref="Tick"/> 的牆鐘假裝。</summary>
        private long _voiceMs = 0L;

        /// <summary>這一輪收到幾個語音幀（&gt;0 ⇒ 語音是真的在走，<see cref="Tick"/> 不再假裝）。</summary>
        private int _frames = 0;

        public ReplyReveal(
            int maxSentences = ReplySplit.SentencesPerBubble,
            int msPerChar = 200,
            long tickMs = 250L)
        {
            _maxSentences = maxSentences;
            _msPerChar = msPerChar;
            _tickMs = tickMs;
        }

        /// <summary>這一輪文字一共會被切成幾段（顯示進度的上限）。</summary>
        public int ParkedCount => _groups.Count;

        /// <summary>現在畫面上有幾個氣泡。</summary>
        public int ShownCount => _shown.Count;

        /// <summary>
        /// 已經**定稿**、可以寫進紀錄的氣泡數（owner 2026-10-01：「按照念的時機一段一段 pop 進去」）。
        ///
        /// 規則：只要「下一條已經出現」，前面那些就不會再變（不變式：只有最後一條會長大）⇒ 它們可以寫了；
        /// **最後那一條**要等 <paramref name="textFinal"/>（文字定稿）才寫——串流中它還會長大，早寫會寫進半句。
        /// 沒被念到的那些永遠不會出現在這裡 ⇒ 被打斷時「後面的直接放棄」是自然結果，不用另外清。
        /// </summary>
        public int ClosedCount(bool textFinal) =>
            textFinal ? _shown.Count : Math.Max(0, _shown.Count - 1);

        /// <summary>畫面上要顯示的氣泡（副本；外殼只讀它）。</summary>
        public List<string> Bubbles() => new List<string>(_shown);

        /// <summary>這一輪開始／被打斷：全部歸零（下一個回覆重新分段）。</summary>
        public void Reset()
        {
            _shown.Clear();
            _groups = new List<string>();
            _bounds = new List<long>();
            _voiceMs = 0L;
            _frames = 0;
        }

        /// <summary>
        /// 這一輪的文字更新（串流中的一段，或這一輪的定稿）。
        ///
        /// 回傳 true ⇒ 畫面上的氣泡該重畫了。
        /// </summary>
        public bool SetText(string text)
        {
            var before = new List<string>(_shown);
            _groups = ReplySplit.Groups(text, _maxSentences);
            _bounds = ReplySplit.BoundsMs(_groups, _msPerChar);
            if (_groups.Count == 0)
            {
                _shown.Clear();
            }
            else if (_shown.Count == 0)
            {
                _shown.Add(_groups[0]);          // 第一段一到就出現
            }
            else
            {
                // 最後那一條還在長：讓它跟著文字長大。只允許「長大」（StartsWith），
                // 重新分段若把它合併或搬家，都不動畫面——否則就是 owner 看到的重排。
                var last = _shown.Count - 1;
                var now = last < _groups.Count ? _groups[last] : null;
                if (now != null && now.StartsWith(_shown[last], StringComparison.Ordinal)) _shown[last] = now;
                Advance();
            }
            return !SameContent(_shown, before);
        }

        /// <summary>語音幀到（橋側每幀帶 `seconds`＝這一幀的實測長度）：把「有聲音的長度」往前推。</summary>
        public bool OnSpeech(double seconds)
        {
            _frames++;
            if (seconds > 0.0) _voiceMs += (long)(seconds * 1000.0);
            return Advance();
        }

        /// <summary>
        /// 沒有語音幀時的節拍：用牆鐘假裝語音在走（不出聲、或這一輪跑本機合成時）。
        ///
        /// ⚠️ 只有「這一輪一個語音幀都還沒來」時才假裝。有幀就交給幀——否則兩邊同時推，氣泡會一次跳兩條。
        /// </summary>
        public bool Tick()
        {
            if (_frames > 0) return false;
            _voiceMs += _tickMs;
            return Advance();
        }

        /// <summary>語音長度走到哪一段的界線，就顯示到那一段。</summary>
        private bool Advance()
        {
            var before = _shown.Count;
            while (_shown.Count < _groups.Count && _voiceMs >= _bounds[_shown.Count - 1])
            {
                _shown.Add(_groups[_shown.Count]);
            }
            return _shown.Count != before;
        }

        /// <summary>逐元素比對（Kotlin 的 `List != List` 是結構比較，不是比參考——這裡照抄語意）。</summary>
        private static bool SameContent(List<string> a, List<string> b)
        {
            if (a.Count != b.Count) return false;
            for (var i = 0; i < a.Count; i++)
            {
                if (!string.Equals(a[i], b[i], StringComparison.Ordinal)) return false;
            }
            return true;
        }
    }
}
