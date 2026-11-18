using System;
using System.Collections.Generic;

namespace BlueAgent.Logic
{
    /// <summary>一則尚未被使用者在對應聊天頁看到的交付。</summary>
    [Serializable]
    public sealed class ConversationItem
    {
        public string Entity = "";
        public string TurnId = "";
        public string Text = "";
        public double At;
        public string Kind = "push";
        /// <summary>Base64 語音只在本次程序內轉交，不塞進 PlayerPrefs；重啟後由文字與歷史補齊。</summary>
        [NonSerialized] public string Audio = "";
    }

    /// <summary>端側暫存的可持久化形狀；Unity 用 JsonUtility 存，純規則層不依賴 Unity。</summary>
    [Serializable]
    public sealed class ConversationStoreSnapshot
    {
        public List<ConversationItem> Pending = new List<ConversationItem>();
        public List<string> Seen = new List<string>();
    }

    /// <summary>
    /// 按 entity 保存「還沒讀的交付」，並為 SSE 重連、歷史與推播重疊去重。
    ///
    /// 這不是聊天歷史的第二份真值：完整歷史仍以伺服器 outbox 為準。這裡只保存有界的
    /// 未讀窗口，解決切頁、切角色與 App 重啟之間的交付遺失。同步游標與已讀位置不混用：
    /// 未讀數直接由尚未 Take 的項目計算，Take 才代表使用者已經進入該角色的聊天頁。
    /// </summary>
    public sealed class ConversationStore
    {
        public enum Route
        {
            Added,
            Duplicate,
            Ignore,
        }

        public const int PendingMaxPerEntity = 64;
        public const int SeenMax = 512;

        private readonly Dictionary<string, List<ConversationItem>> _pending =
            new Dictionary<string, List<ConversationItem>>();
        private readonly HashSet<string> _seen = new HashSet<string>();
        private readonly Queue<string> _seenOrder = new Queue<string>();

        public ConversationStore(ConversationStoreSnapshot snapshot = null)
        {
            if (snapshot == null) return;
            if (snapshot.Seen != null)
                foreach (var id in snapshot.Seen)
                    if (!string.IsNullOrEmpty(id)) Remember(id);
            if (snapshot.Pending == null) return;
            foreach (var item in snapshot.Pending)
            {
                if (item == null || string.IsNullOrEmpty(item.Entity) || string.IsNullOrEmpty(item.Text)) continue;
                if (!_pending.TryGetValue(item.Entity, out var rows))
                {
                    rows = new List<ConversationItem>();
                    _pending[item.Entity] = rows;
                }
                rows.Add(Copy(item));
                Trim(item.Entity);
            }
        }

        public Route Accept(string entity, string turnId, string text, double at = 0,
                            string kind = "push", string audio = "")
        {
            if (string.IsNullOrEmpty(entity) || string.IsNullOrEmpty(text)) return Route.Ignore;
            if (!string.IsNullOrEmpty(turnId))
            {
                if (_seen.Contains(turnId)) return Route.Duplicate;
                Remember(turnId);
            }
            if (!_pending.TryGetValue(entity, out var rows))
            {
                rows = new List<ConversationItem>();
                _pending[entity] = rows;
            }
            rows.Add(new ConversationItem
            {
                Entity = entity,
                TurnId = turnId ?? "",
                Text = text,
                At = at,
                Kind = string.IsNullOrEmpty(kind) ? "push" : kind,
                Audio = audio ?? "",
            });
            Trim(entity);
            return Route.Added;
        }

        public int Unread(string entity)
        {
            if (string.IsNullOrEmpty(entity)) return 0;
            return _pending.TryGetValue(entity, out var rows) ? rows.Count : 0;
        }

        public int TotalUnread()
        {
            var count = 0;
            foreach (var rows in _pending.Values) count += rows.Count;
            return count;
        }

        /// <summary>只看這個角色的未讀副本，不標已讀；畫面全部渲染成功後才 Take。</summary>
        public List<ConversationItem> Preview(string entity)
        {
            var preview = new List<ConversationItem>();
            if (string.IsNullOrEmpty(entity) || !_pending.TryGetValue(entity, out var rows))
                return preview;
            foreach (var item in rows) preview.Add(Copy(item));
            return preview;
        }

        /// <summary>交出這個角色的未讀並標為已讀；順序與接收順序一致。</summary>
        public List<ConversationItem> Take(string entity)
        {
            if (string.IsNullOrEmpty(entity) || !_pending.TryGetValue(entity, out var rows))
                return new List<ConversationItem>();
            _pending.Remove(entity);
            return rows;
        }

        public ConversationStoreSnapshot Snapshot()
        {
            var snapshot = new ConversationStoreSnapshot();
            foreach (var rows in _pending.Values)
                foreach (var item in rows) snapshot.Pending.Add(Copy(item));
            snapshot.Seen.AddRange(_seenOrder);
            return snapshot;
        }

        private void Remember(string turnId)
        {
            _seen.Add(turnId);
            _seenOrder.Enqueue(turnId);
            while (_seenOrder.Count > SeenMax) _seen.Remove(_seenOrder.Dequeue());
        }

        private void Trim(string entity)
        {
            var rows = _pending[entity];
            while (rows.Count > PendingMaxPerEntity) rows.RemoveAt(0);
        }

        private static ConversationItem Copy(ConversationItem item)
        {
            return new ConversationItem
            {
                Entity = item.Entity ?? "",
                TurnId = item.TurnId ?? "",
                Text = item.Text ?? "",
                At = item.At,
                Kind = item.Kind ?? "push",
                Audio = item.Audio ?? "",
            };
        }
    }
}
