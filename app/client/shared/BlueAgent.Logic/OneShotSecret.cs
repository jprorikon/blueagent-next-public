namespace BlueAgent.Logic
{
    /// <summary>
    /// 「只顯示這一次」的明文（簽發／輪替回來的 guest token）——unity-review U-5，2026-10-28。
    ///
    /// 契約：明文只活在**簽發的那一次進頁**裡。離開帳號頁再進來、登出再登入，都不能再看到。
    /// 以前是靜態字串、進頁不清 ⇒ 換頁回來照樣畫出舊 token。
    /// </summary>
    public sealed class OneShotSecret
    {
        public string Value { get; private set; } = "";
        public string Label { get; private set; } = "";

        public bool HasValue { get { return Value.Length > 0; } }

        /// <summary>重新進頁：上一次進頁留下的明文作廢（同一次進頁裡的重畫不要叫這個）。</summary>
        public void BeginVisit()
        {
            Forget();
        }

        public void Put(string value, string label)
        {
            Value = value ?? "";
            Label = label ?? "";
        }

        /// <summary>登出／回登入頁：一律清掉。</summary>
        public void Forget()
        {
            Value = "";
            Label = "";
        }
    }
}
