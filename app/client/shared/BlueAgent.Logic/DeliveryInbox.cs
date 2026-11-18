using System.Collections.Generic;

namespace BlueAgent.Logic
{
    /// <summary>
    /// 常駐推播（`delivery`）的分流：現在這一頁的角色就顯示、別的角色先收著（unity-review U-4，2026-10-28）。
    ///
    /// 推播是**按 principal** 發的（同一個主人的所有角色共用一條線）。以前前景看著 A 時，
    /// B 的主動訊息直接 return（只有背景才發系統通知）⇒ 切到 B 也看不到。
    /// 現在：別人的先按角色排隊、記未讀數；切到那個角色時依原順序交出來。
    /// 同一個 `turn_id` 只算一次（重連重播、歷史與推播重疊都靠這個去重）。
    /// </summary>
    public sealed class DeliveryInbox
    {
        public enum Route
        {
            /// <summary>這一頁的角色：直接畫。</summary>
            Show,
            /// <summary>別的角色：收進那個角色的未讀。</summary>
            Stash,
            /// <summary>同一個 `turn_id` 已經處理過。</summary>
            Duplicate,
            /// <summary>沒有文字（控制幀之類）：不歸這裡管。</summary>
            Ignore,
        }

        public sealed class Item
        {
            public string Entity = "";
            public string TurnId = "";
            public string Text = "";
        }

        /// <summary>去重表的上限（超過就忘掉最舊的；重播窗口本來就有界）。</summary>
        public const int SeenMax = 512;

        private readonly Dictionary<string, List<Item>> _stash = new Dictionary<string, List<Item>>();
        private readonly HashSet<string> _seen = new HashSet<string>();
        private readonly Queue<string> _seenOrder = new Queue<string>();

        public Route Accept(string currentEntity, string from, string turnId, string text)
        {
            if (string.IsNullOrEmpty(text)) return Route.Ignore;
            if (!string.IsNullOrEmpty(turnId))
            {
                if (_seen.Contains(turnId)) return Route.Duplicate;
                Remember(turnId);
            }
            if (string.IsNullOrEmpty(from) || from == currentEntity) return Route.Show;
            if (!_stash.TryGetValue(from, out var rows))
            {
                rows = new List<Item>();
                _stash[from] = rows;
            }
            rows.Add(new Item { Entity = from, TurnId = turnId ?? "", Text = text });
            return Route.Stash;
        }

        public int Unread(string entity)
        {
            if (string.IsNullOrEmpty(entity)) return 0;
            return _stash.TryGetValue(entity, out var rows) ? rows.Count : 0;
        }

        /// <summary>交出這個角色收著的那幾條（依收到順序），交完就清。</summary>
        public List<Item> Take(string entity)
        {
            if (string.IsNullOrEmpty(entity) || !_stash.TryGetValue(entity, out var rows))
                return new List<Item>();
            _stash.Remove(entity);
            return rows;
        }

        private void Remember(string turnId)
        {
            _seen.Add(turnId);
            _seenOrder.Enqueue(turnId);
            while (_seenOrder.Count > SeenMax) _seen.Remove(_seenOrder.Dequeue());
        }
    }
}
