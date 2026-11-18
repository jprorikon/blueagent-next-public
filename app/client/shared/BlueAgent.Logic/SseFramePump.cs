using System;
using System.Collections.Generic;

namespace BlueAgent.Logic
{
    /// <summary>切好的一幀（`SseStream` 的產物）。</summary>
    public struct SseFrame
    {
        public string Id;
        public string Event;
        public string Data;

        public override string ToString()
        {
            return (Event ?? "") + "#" + (Id ?? "");
        }
    }

    /// <summary>
    /// 逐幀派送的**唯一入口**：一幀處理失敗，不准把整批後面的幀吃掉、也不准把游標推過去。
    ///
    /// 為什麼要這一支（2026-11-04 取證）：`NextApi` 以前是
    ///
    ///     Cursors.Advance(wire.Id);
    ///     if (onEvent != null) onEvent(wire.Event, json);   // ← 這裡一拋，整批就斷
    ///
    /// ⇒ 某幀的處理器拋例外（實測：切到設定頁時她主動說話，`BuildBubble` 撞到已銷毀的 `_list`
    /// 拋 `MissingReferenceException`）時，**同一批裡後面的幀被跳過、而游標已經推過去**，
    /// 重連用 `Last-Event-ID` 也補不回來 ⇒ 永久丟訊息。
    ///
    /// 現在：**處理成功才推進游標，而且游標不能越過失敗缺口**。失敗後的幀仍照常處理，
    /// 但在那個缺口補上之前都不再推進游標；否則第 2 幀失敗、第 3 幀成功時會把游標推到 3，
    /// 重連反而永遠補不到 2。重放產生的重複由客戶端既有的 `turn_id` 去重擋掉。
    /// 語義從「一幀炸掉整批丟」變成「一幀失敗、下次補上」（at-least-once ＋ 去重）。
    ///
    /// 放在共用邏輯層的理由：這是**純規則**，兩端（手機 Unity／其它殼）都適用，而且可以離線測。
    /// </summary>
    public static class SseFramePump
    {
        /// <param name="frames">這一批切好的幀（依伺服器送來順序）。</param>
        /// <param name="handle">處理一幀；回 <c>true</c> ＝這幀算完成（可以推進游標）。</param>
        /// <param name="advance">推進游標（通常接 `EventCursors.Advance`）。</param>
        /// <param name="onError">某幀拋例外時的通知（不影響後面的幀）。</param>
        /// <returns>成功處理的幀數（診斷用）。</returns>
        public static int Pump(IList<SseFrame> frames,
                               Func<SseFrame, bool> handle,
                               Action<string> advance,
                               Action<SseFrame, Exception> onError = null)
        {
            var cursorBlocked = false;
            return Pump(frames, handle, advance, ref cursorBlocked, onError);
        }

        /// <summary>串流版：缺口旗標由呼叫端在同一條連線的各批次之間保留；重連時重新開始。</summary>
        /// <param name="cursorBlocked">本連線是否已有失敗缺口；一旦堵住，之後成功幀也只處理、不推游標。</param>
        public static int Pump(IList<SseFrame> frames,
                               Func<SseFrame, bool> handle,
                               Action<string> advance,
                               ref bool cursorBlocked,
                               Action<SseFrame, Exception> onError = null)
        {
            if (frames == null || frames.Count == 0) return 0;
            var done = 0;
            for (var i = 0; i < frames.Count; i++)
            {
                var frame = frames[i];
                var ok = true;
                try
                {
                    ok = handle == null || handle(frame);
                }
                catch (Exception error)
                {
                    ok = false;
                    cursorBlocked = true;
                    if (onError != null)
                    {
                        // 錯誤處理自己再炸也不能吃掉後面的幀
                        try { onError(frame, error); }
                        catch (Exception) { }
                    }
                }
                // false 是 ready 這類「已處理但不帶游標」的控制幀，不是失敗缺口。
                if (!ok) continue;
                done++;
                if (!cursorBlocked && advance != null) advance(frame.Id);
            }
            return done;
        }
    }
}
