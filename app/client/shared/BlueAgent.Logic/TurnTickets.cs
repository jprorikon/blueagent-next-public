namespace BlueAgent.Logic
{
    /// <summary>一輪送出的身份證：哪一號、替哪個角色送的（建立後不變）。</summary>
    public sealed class TurnTicket
    {
        public readonly int Id;
        public readonly string Entity;

        public TurnTicket(int id, string entity)
        {
            Id = id;
            Entity = entity ?? "";
        }
    }

    /// <summary>
    /// 聊天頁「現在這一輪」是哪一張票（unity-review U-2，2026-10-28）。
    ///
    /// 以前所有回合共用一個可變的 `_turnEntity`，回呼拿「現在的」`_turnEntity == _entityId` 判斷來源：
    /// A 送出 → 換到 B → B 送出（`_turnEntity` 變 B、A 被 Abort）→ A 的收尾晚一幀才回來，
    /// 看到的已經是 B ⇒ A 的「斷線」寫進 B 的佔位泡泡、B 的忙碌旗標被提前清掉。
    /// 現在每一輪拿一張票，回呼只認**自己那張**是不是還是現在這一張。
    /// </summary>
    public sealed class TurnTickets
    {
        private int _seq;

        public TurnTicket Current { get; private set; }

        public TurnTicket Begin(string entity)
        {
            _seq++;
            Current = new TurnTicket(_seq, entity);
            return Current;
        }

        public bool IsCurrent(TurnTicket ticket)
        {
            return ticket != null && ReferenceEquals(ticket, Current);
        }

        /// <summary>換角色／登出：在途那一輪的回呼全部作廢。</summary>
        public void Invalidate()
        {
            Current = null;
        }
    }

    /// <summary>
    /// 「只有擁有者能清」的單格句柄（U-2：`NextApi._turnRequest`）。
    /// 以前舊協程收尾無條件 `_turnRequest = null`，把新一輪的句柄清掉 ⇒ 之後取消不了。
    /// </summary>
    public sealed class OwnedSlot<T> where T : class
    {
        public T Value { get; private set; }

        public void Set(T value)
        {
            Value = value;
        }

        /// <summary>格子裡還是 <paramref name="owner"/> 才清；回 true＝清掉了。</summary>
        public bool ClearIf(T owner)
        {
            if (owner == null || !ReferenceEquals(Value, owner)) return false;
            Value = null;
            return true;
        }

        public T Take()
        {
            var value = Value;
            Value = null;
            return value;
        }
    }
}
