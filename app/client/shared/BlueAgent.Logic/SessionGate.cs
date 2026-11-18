namespace BlueAgent.Logic
{
    /// <summary>
    /// 端末的「登入期」代次＋ 401 收口（unity-review U-3／U-6，2026-10-28）。
    ///
    /// 每次登入 <see cref="Open"/>、登出 <see cref="Close"/> 都換一個代次。非同步的回呼（請求、
    /// 位置推送、補拿）出發時記下代次，回來時代次不一樣 ⇒ 屬於上一段登入，**什麼都不准動**
    /// （以前登出只清 PlayerPrefs：位置迴圈照跑、`Api.Token` 還是舊的、舊請求的回呼寫進新畫面）。
    ///
    /// 401 統一在這裡判：同一段登入**只觸發一次**（好幾個在途請求同時 401，只回一次登入頁）；
    /// 403 不算（契約 §4：提示無權限，不清 token）；登入前（`/api/me` 驗 token 那一下）的 401
    /// 由登入頁自己講，不走這裡。
    /// </summary>
    public sealed class SessionGate
    {
        private bool _expired;

        public int Epoch { get; private set; }
        public bool Active { get; private set; }

        /// <summary>登入成功：開新的一段，回它的代次。</summary>
        public int Open()
        {
            Epoch++;
            Active = true;
            _expired = false;
            return Epoch;
        }

        /// <summary>登出／token 失效：這一段結束，之前出發的回呼一律作廢。</summary>
        public void Close()
        {
            Epoch++;
            Active = false;
        }

        /// <summary>出發時的代次還是現在這一段（登入前出發的也算——登入頁自己的請求要回得來）。</summary>
        public bool IsCurrent(int epoch)
        {
            return epoch == Epoch;
        }

        /// <summary>出發時的代次是**登入中**的這一段。</summary>
        public bool IsLive(int epoch)
        {
            return Active && epoch == Epoch;
        }

        /// <summary>
        /// 回 true ＝「這一下要清 token 回登入頁」：只有 401、只有登入中這一段的請求、每段只一次。
        /// </summary>
        public bool ReportStatus(int status, int epoch)
        {
            if (status != 401 || !IsLive(epoch) || _expired) return false;
            _expired = true;
            return true;
        }
    }
}
