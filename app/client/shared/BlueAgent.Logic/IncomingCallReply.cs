namespace BlueAgent.Logic
{
    /// <summary>
    /// 她打來的電話，主人按了「応答」／「拒否」之後，伺服器的回應怎麼收（審核 OC-1，2026-11-17）。
    ///
    /// 以前一按就把這一通標成「處理過」、不管回什麼都收起畫面 ⇒ 網路一抖（status 0／5xx），伺服器還在響，
    /// 這一通卻再也叫不出來，只能等 30 秒逾時。現在只有兩種情況算處理完：
    /// <list type="bullet">
    /// <item><see cref="Kind.Done"/>：成功（2xx）。</item>
    /// <item><see cref="Kind.Gone"/>：伺服器明說這一通沒了（404 找不到、410 已結束）——收起、不再彈。</item>
    /// </list>
    /// 其餘一律 <see cref="Kind.Retry"/>：畫面留著、按鈕恢復、鈴聲繼續，主人可以再按一次；
    /// 這一通若其實已經結束，響鈴輪詢（`/state`）會照常把它收掉。
    /// </summary>
    public static class IncomingCallReply
    {
        public enum Kind { Done, Gone, Retry }

        public static Kind Classify(bool ok, int status)
        {
            if (ok) return Kind.Done;
            if (status == 404 || status == 410) return Kind.Gone;
            return Kind.Retry;
        }
    }
}
