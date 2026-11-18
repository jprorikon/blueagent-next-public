using System;
using System.Collections.Generic;
using System.Security.Cryptography;
using System.Text;

namespace BlueAgent.Logic
{
    /// <summary>Unity 的完整本地聊天副本；網路回包與事件都先落這裡再交給 UI。</summary>
    public sealed class ChatItem
    {
        public JObject Json { get; internal set; }
        public long LocalSeq { get; internal set; }
        public bool Pending { get; internal set; }
        public string MutationId { get; internal set; } = "";
    }

    public sealed class ChatMutation
    {
        public string Id = "";
        public string ConversationId = "";
        public JArray Parts;
        public string State = "pending";
        public int Attempts;
        public string Error = "";
        public double UpdatedAt;
    }

    /// <summary>S3 conversation/message/part/outbox 真 SQLite；不是 PlayerPrefs 或有界快取。</summary>
    public sealed class ChatRepository : IDisposable
    {
        private const string Schema = @"
CREATE TABLE IF NOT EXISTS conversation(
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS message(
  local_seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
  conversation_id TEXT NOT NULL, server_seq INTEGER NOT NULL DEFAULT 0,
  author_id TEXT NOT NULL, at REAL NOT NULL, payload TEXT NOT NULL,
  pending INTEGER NOT NULL DEFAULT 0, mutation_id TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS message_history ON message(conversation_id, local_seq);
CREATE INDEX IF NOT EXISTS message_timeline ON message(conversation_id, at, local_seq);
CREATE TABLE IF NOT EXISTS message_part(
  message_id TEXT NOT NULL, part_index INTEGER NOT NULL, kind TEXT NOT NULL,
  text TEXT NOT NULL DEFAULT '', media_id TEXT NOT NULL DEFAULT '',
  sticker TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(message_id, part_index));
CREATE TABLE IF NOT EXISTS chat_outbox(
  client_mutation_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, parts TEXT NOT NULL,
  state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
  result_json TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS chat_outbox_ready ON chat_outbox(state, created_at, client_mutation_id);
CREATE TABLE IF NOT EXISTS sync_cursor(scope TEXT PRIMARY KEY, cursor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS applied_event(event_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS read_position(conversation_id TEXT PRIMARY KEY, message_id TEXT NOT NULL);";

        private readonly SqliteDb _db;

        public ChatRepository(string path)
        {
            _db = new SqliteDb(path);
            foreach (var statement in Schema.Split(';'))
                if (!string.IsNullOrWhiteSpace(statement)) _db.Execute(statement);
        }

        public void Dispose() { _db.Dispose(); }

        public static string DirectId(string first, string second)
        {
            var pair = string.CompareOrdinal(first, second) <= 0
                ? first + "\0" + second : second + "\0" + first;
            using (var sha = SHA256.Create())
            {
                var bytes = sha.ComputeHash(Encoding.UTF8.GetBytes(pair));
                var hex = new StringBuilder(24);
                for (var i = 0; i < 12; i++) hex.Append(bytes[i].ToString("x2"));
                return "cnv_" + hex;
            }
        }

        /// <summary>群聊在本地副本中的路由键；与单个 entity 的 1:1 会话严格分开。</summary>
        public static string GroupKey(string conversationId) { return "group:" + (conversationId ?? ""); }
        public static bool IsGroupKey(string route)
        {
            return !string.IsNullOrEmpty(route) && route.StartsWith("group:", StringComparison.Ordinal);
        }

        public JObject EnsureConversation(string entityId, string accountId, string display = "")
        {
            var id = DirectId(accountId, entityId);
            var found = Conversation(entityId);
            if (found != null) return found;
            var participants = JArray.OfStrings(new[] { accountId, entityId });
            var json = new JObject().Put("id", id).Put("kind", "direct")
                .Put("created_at", Now()).Put("participants", participants);
            _db.Execute("INSERT OR IGNORE INTO conversation VALUES (?,?,?,?)", id, entityId ?? "",
                json.ToString(), Now());
            return json;
        }

        public JObject Conversation(string entityId)
        {
            var rows = _db.Query("SELECT payload FROM conversation WHERE entity_id=?", entityId ?? "");
            return rows.Count == 0 ? null : new JObject(rows[0].String("payload"));
        }

        public List<JObject> Conversations()
        {
            var rows = _db.Query("SELECT payload FROM conversation ORDER BY updated_at DESC,id");
            var result = new List<JObject>();
            foreach (var row in rows) result.Add(new JObject(row.String("payload")));
            return result;
        }

        public JObject ConversationById(string conversationId)
        {
            var rows = _db.Query("SELECT payload FROM conversation WHERE id=?", conversationId ?? "");
            return rows.Count == 0 ? null : new JObject(rows[0].String("payload"));
        }

        /// <summary>服務端會話摘要也進同一份副本；principal 必須是 /api/me 的 canonical account_id。</summary>
        public string MergeConversation(JObject incoming, string principal)
        {
            var entity = "";
            _db.Transaction(() => { entity = MergeConversationLocked(incoming, principal); });
            return entity;
        }

        public void MergeConversations(JArray conversations, string principal)
        {
            _db.Transaction(() =>
            {
                for (var i = 0; conversations != null && i < conversations.Length(); i++)
                    MergeConversationLocked(conversations.OptJObject(i), principal);
            });
        }

        private string MergeConversationLocked(JObject incoming, string principal)
        {
            if (incoming == null) return "";
            var id = incoming.OptString("id");
            if (string.IsNullOrEmpty(id)) return "";
            var group = incoming.OptString("kind") == "group";
            var entity = group ? GroupKey(id) : "";
            var participants = incoming.OptJArray("participants");
            for (var i = 0; !group && participants != null && i < participants.Length(); i++)
            {
                var account = participants.OptString(i);
                if (!string.IsNullOrEmpty(account) && account != principal) entity = account;
            }
            if (string.IsNullOrEmpty(entity)) return "";
            var lastId = incoming.OptJObject("last_message")?.OptString("id") ?? "";
            if (!string.IsNullOrEmpty(lastId) && ReadPosition(id) == lastId) incoming.Put("unread", 0);
            if (ConversationById(id) == null)
            {
                if (group)
                {
                    _db.Execute("INSERT OR IGNORE INTO conversation VALUES (?,?,?,?)", id, entity,
                        incoming.ToString(), incoming.OptJObject("last_message")?.OptDouble("created_at")
                            ?? incoming.OptDouble("created_at"));
                }
                else
                {
                    var local = Conversation(entity);
                    if (local == null) EnsureConversation(entity, principal);
                    var localId = local?.OptString("id") ?? DirectId(principal, entity);
                    if (localId != id) RekeyConversation(localId, id);
                }
            }
            _db.Execute("UPDATE conversation SET entity_id=?,payload=?,updated_at=? WHERE id=?",
                entity, incoming.ToString(), incoming.OptJObject("last_message")?.OptDouble("created_at")
                    ?? incoming.OptDouble("created_at"), id);
            return entity;
        }

        /// <summary>舊版把 legacy principal 算進 direct id；升級時連同本地訊息與未送 Outbox 一起改掛服務端 id。</summary>
        private void RekeyConversation(string oldId, string newId)
        {
            if (string.IsNullOrEmpty(oldId) || oldId == newId || ConversationById(oldId) == null) return;
            var messages = _db.Query("SELECT id,payload FROM message WHERE conversation_id=?", oldId);
            foreach (var row in messages)
            {
                var payload = new JObject(row.String("payload")).Put("conversation_id", newId);
                _db.Execute("UPDATE message SET conversation_id=?,payload=? WHERE id=?",
                    newId, payload.ToString(), row.String("id"));
            }
            _db.Execute("UPDATE chat_outbox SET conversation_id=? WHERE conversation_id=?", newId, oldId);
            _db.Execute("UPDATE sync_cursor SET scope=? WHERE scope=?", newId, oldId);
            _db.Execute("UPDATE read_position SET conversation_id=? WHERE conversation_id=?", newId, oldId);
            _db.Execute("UPDATE conversation SET id=? WHERE id=?", newId, oldId);
        }

        public ChatItem Enqueue(string entityId, string accountId, string display, JArray parts,
                                string mutationId, double at)
        {
            if (string.IsNullOrEmpty(mutationId)) throw new ArgumentException("mutation id is empty");
            if (parts == null || parts.Length() == 0) throw new ArgumentException("parts are empty");
            ChatItem result = null;
            _db.Transaction(() =>
            {
                var existing = Mutation(mutationId);
                if (existing != null)
                {
                    result = FindByMutation(mutationId);
                    return;
                }
                // 已打开的群直接复用原 conversation id；只有未知路由才建立 1:1 本地占位。
                var conversation = Conversation(entityId) ?? EnsureConversation(entityId, accountId, display);
                var conversationId = conversation.OptString("id");
                var messageId = "local_" + mutationId;
                var message = new JObject().Put("id", messageId).Put("seq", 0)
                    .Put("conversation_id", conversationId).Put("created_at", at)
                    .Put("author", new JObject().Put("id", accountId).Put("kind", "owner").Put("display", display ?? ""))
                    .Put("parts", parts);
                InsertMessage(message, true, mutationId);
                _db.Execute("INSERT INTO chat_outbox(client_mutation_id,conversation_id,parts,state,created_at,updated_at) " +
                            "VALUES (?,?,?,'pending',?,?)", mutationId, conversationId, parts.ToString(), at, at);
                result = new ChatItem { Json = message, Pending = true, MutationId = mutationId };
            });
            return result;
        }

        public List<ChatItem> Messages(string entityId, int limit = 50, string beforeId = "")
        {
            var conversation = Conversation(entityId);
            if (conversation == null) return new List<ChatItem>();
            var conversationId = conversation.OptString("id");
            var count = Math.Max(1, Math.Min(500, limit));
            // 時間軸＝訊息時間 `at`（同一刻再按本地插入序）。⚠️ 不能只按 local_seq：
            // 先收到即時新訊息、後補拉舊歷史時，舊訊息的 local_seq 反而較大，
            // 「最近 N 條」就會變成舊的那批、最新的被擠出視窗（2026-11-07 模擬器實測）。
            List<SqliteDb.Row> rows;
            if (string.IsNullOrEmpty(beforeId))
            {
                rows = _db.Query("SELECT local_seq,payload,pending,mutation_id FROM message " +
                    "WHERE conversation_id=? ORDER BY at DESC, local_seq DESC LIMIT " + count,
                    conversationId);
            }
            else
            {
                var found = _db.Query("SELECT local_seq,at FROM message WHERE id=? AND conversation_id=?",
                    beforeId, conversationId);
                if (found.Count == 0) return new List<ChatItem>();
                var anchorAt = found[0].Double("at");
                var anchorSeq = found[0].Long("local_seq");
                rows = _db.Query("SELECT local_seq,payload,pending,mutation_id FROM message " +
                    "WHERE conversation_id=? AND (at<? OR (at=? AND local_seq<?)) " +
                    "ORDER BY at DESC, local_seq DESC LIMIT " + count,
                    conversationId, anchorAt, anchorAt, anchorSeq);
            }
            rows.Reverse();
            return ReadItems(rows);
        }

        public List<ChatMutation> Pending()
        {
            var rows = _db.Query("SELECT * FROM chat_outbox WHERE state='pending' ORDER BY created_at,client_mutation_id");
            var result = new List<ChatMutation>();
            foreach (var row in rows) result.Add(ReadMutation(row));
            return result;
        }

        public ChatMutation Mutation(string id)
        {
            var rows = _db.Query("SELECT * FROM chat_outbox WHERE client_mutation_id=?", id ?? "");
            return rows.Count == 0 ? null : ReadMutation(rows[0]);
        }

        public void MarkAttempt(string id, double at)
        {
            _db.Execute("UPDATE chat_outbox SET attempts=attempts+1,updated_at=? WHERE client_mutation_id=? AND state='pending'",
                at, id ?? "");
        }

        public void MarkFailed(string id, string error, bool retry, double at)
        {
            _db.Execute("UPDATE chat_outbox SET state=?,last_error=?,updated_at=? WHERE client_mutation_id=? AND state<>'accepted'",
                retry ? "pending" : "failed", error ?? "unknown", at, id ?? "");
        }

        public void RetryFailed(double at)
        {
            _db.Execute("UPDATE chat_outbox SET state='pending',last_error='',updated_at=? WHERE state='failed'", at);
        }

        public void MarkAccepted(string mutationId, JObject response, double at)
        {
            _db.Transaction(() =>
            {
                var mutation = Mutation(mutationId);
                if (mutation == null || mutation.State == "accepted") return;
                var message = response == null ? null : response.OptJObject("message");
                if (message == null) throw new InvalidOperationException("accepted response has no message");
                var localId = "local_" + mutationId;
                var serverId = message.OptString("id");
                _db.Execute("UPDATE chat_outbox SET state='accepted',result_json=?,last_error='',updated_at=? " +
                            "WHERE client_mutation_id=?", response.ToString(), at, mutationId);
                UpsertMessage(message);
                _db.Execute("DELETE FROM message_part WHERE message_id=?", localId);
                _db.Execute("DELETE FROM message WHERE id=?", localId);
            });
        }

        public void MergeMessages(JArray messages)
        {
            _db.Transaction(() =>
            {
                for (var i = 0; messages != null && i < messages.Length(); i++)
                {
                    var message = messages.OptJObject(i);
                    if (message != null) UpsertMessage(message);
                }
            });
        }

        /// <param name="reader">本機帳號 id：給了就替**群**累加未讀（自己說的不算）。</param>
        public void ApplyEvent(JObject envelope, string reader = "")
        {
            var type = envelope == null ? "" : envelope.OptString("type");
            if (type != "chat.message.created" && type != "chat.message.deleted") return;
            var eventId = envelope.OptString("event_id");
            if (string.IsNullOrEmpty(eventId)) throw new ArgumentException("event id is empty");
            _db.Transaction(() =>
            {
                _db.Execute("INSERT OR IGNORE INTO applied_event VALUES (?)", eventId);
                if (_db.ScalarLong("SELECT changes() AS value") == 0) return;
                var message = envelope.OptJObject("payload")?.OptJObject("message");
                if (message == null) return;
                if (type == "chat.message.deleted")
                {
                    ApplyTombstoneLocked(message);       // 收回：只改本地已有的那一則，不算未讀
                    return;
                }
                UpsertMessage(message);
                if (!string.IsNullOrEmpty(reader)) BumpGroupUnreadLocked(message, reader);
            });
        }

        /// <summary>
        /// 群的未讀＝服務端摘要的 `unread`（按讀游標算），兩次列表同步之間由事件累加（2026-11-08 接上；
        /// 以前群那一行固定 0）。只在事件**第一次**落庫時算一次；下一次列表同步會用服務端值覆蓋。
        /// 1:1 仍走 ConversationStore，這裡不碰。
        /// </summary>
        private void BumpGroupUnreadLocked(JObject message, string reader)
        {
            var conversationId = message.OptString("conversation_id");
            var author = message.OptJObject("author")?.OptString("id") ?? message.OptString("author_id");
            if (string.IsNullOrEmpty(conversationId) || author == reader) return;
            var rows = _db.Query("SELECT payload FROM conversation WHERE id=?", conversationId);
            if (rows.Count == 0) return;
            var conversation = new JObject(rows[0].String("payload"));
            if (conversation.OptString("kind") != "group") return;
            if (ReadPosition(conversationId) == message.OptString("id")) return;
            conversation.Put("unread", conversation.OptInt("unread") + 1);      // last_message 由 UpsertMessage 維護
            _db.Execute("UPDATE conversation SET payload=? WHERE id=?", conversation.ToString(), conversationId);
        }

        /// <summary>所有群的未讀總數（頂欄徽章用）。</summary>
        public int GroupUnread()
        {
            var total = 0;
            foreach (var conversation in Conversations())
                if (conversation.OptString("kind") == "group") total += Math.Max(0, conversation.OptInt("unread"));
            return total;
        }

        public string Cursor(string conversationId)
        {
            var rows = _db.Query("SELECT cursor FROM sync_cursor WHERE scope=?", conversationId ?? "");
            return rows.Count == 0 ? "0" : rows[0].String("cursor");
        }

        public void SetCursor(string conversationId, string cursor)
        {
            _db.Execute("INSERT INTO sync_cursor VALUES (?,?) ON CONFLICT(scope) DO UPDATE SET cursor=excluded.cursor",
                conversationId ?? "", cursor ?? "0");
        }

        public string ReadPosition(string conversationId)
        {
            var rows = _db.Query("SELECT message_id FROM read_position WHERE conversation_id=?", conversationId ?? "");
            return rows.Count == 0 ? "" : rows[0].String("message_id");
        }

        /// <summary>打開會話先落本地已讀；服務端確認由 NextChatSync 另做且可重試。</summary>
        public void MarkRead(string conversationId, string messageId)
        {
            if (string.IsNullOrEmpty(conversationId) || string.IsNullOrEmpty(messageId) ||
                messageId.StartsWith("local_", StringComparison.Ordinal)) return;
            _db.Transaction(() =>
            {
                _db.Execute("INSERT INTO read_position VALUES (?,?) ON CONFLICT(conversation_id) " +
                            "DO UPDATE SET message_id=excluded.message_id", conversationId, messageId);
                var rows = _db.Query("SELECT payload FROM conversation WHERE id=?", conversationId);
                if (rows.Count == 0) return;
                var conversation = new JObject(rows[0].String("payload")).Put("unread", 0);
                _db.Execute("UPDATE conversation SET payload=? WHERE id=?", conversation.ToString(), conversationId);
            });
        }

        /// <summary>
        /// 收回的訊息（App Server 的墓碑：同 id、內容清空、<c>deleted=true</c>）蓋掉本地那一則。
        /// 本地沒有就不新增（之後拉歷史時會帶回墓碑）；會話的最後一則正是它才換成墓碑——
        /// 不能像 <see cref="UpsertMessage"/> 那樣一律寫成 last_message，收回舊訊息會把最後一則倒退回去。
        /// </summary>
        public void ApplyTombstone(JObject message)
        {
            if (message == null) return;
            _db.Transaction(() => ApplyTombstoneLocked(message));
        }

        private void ApplyTombstoneLocked(JObject message)
        {
            var id = message.OptString("id");
            if (string.IsNullOrEmpty(id)) return;
            var rows = _db.Query("SELECT conversation_id FROM message WHERE id=?", id);
            if (rows.Count == 0) return;
            _db.Execute("UPDATE message SET payload=?,pending=0,mutation_id='' WHERE id=?", message.ToString(), id);
            _db.Execute("DELETE FROM message_part WHERE message_id=?", id);
            var conversationId = rows[0].String("conversation_id");
            var found = _db.Query("SELECT payload FROM conversation WHERE id=?", conversationId);
            if (found.Count == 0) return;
            var conversation = new JObject(found[0].String("payload"));
            if (conversation.OptJObject("last_message")?.OptString("id") != id) return;
            _db.Execute("UPDATE conversation SET payload=? WHERE id=?",
                conversation.Put("last_message", message).ToString(), conversationId);
        }

        private void UpsertMessage(JObject message)
        {
            var id = message.OptString("id");
            var conversationId = message.OptString("conversation_id");
            if (string.IsNullOrEmpty(id) || string.IsNullOrEmpty(conversationId)) return;
            var author = message.OptJObject("author");
            _db.Execute(@"INSERT INTO message(id,conversation_id,server_seq,author_id,at,payload,pending,mutation_id)
                VALUES (?,?,?,?,?,?,0,'') ON CONFLICT(id) DO UPDATE SET server_seq=excluded.server_seq,
                author_id=excluded.author_id,at=excluded.at,payload=excluded.payload,pending=0,mutation_id=''",
                id, conversationId, (long)message.OptDouble("seq"), author?.OptString("id") ?? "",
                message.OptDouble("created_at"), message.ToString());
            _db.Execute("DELETE FROM message_part WHERE message_id=?", id);
            InsertParts(id, message.OptJArray("parts"));
            var participants = message.OptJObject("payload")?.OptJArray("participants");
            var rows = _db.Query("SELECT entity_id,payload FROM conversation WHERE id=?", conversationId);
            if (rows.Count > 0)
            {
                var conversation = new JObject(rows[0].String("payload")).Put("last_message", message)
                    .Put("created_at", rows.Count > 0 ? message.OptDouble("created_at") : Now());
                _db.Execute("UPDATE conversation SET payload=?,updated_at=? WHERE id=?",
                    conversation.ToString(), message.OptDouble("created_at"), conversationId);
            }
        }

        private void InsertMessage(JObject message, bool pending, string mutationId)
        {
            var id = message.OptString("id");
            _db.Execute("INSERT INTO message(id,conversation_id,server_seq,author_id,at,payload,pending,mutation_id) " +
                        "VALUES (?,?,?,?,?,?,?,?)", id, message.OptString("conversation_id"),
                (long)message.OptDouble("seq"), message.OptJObject("author")?.OptString("id") ?? "",
                message.OptDouble("created_at"), message.ToString(), pending ? 1 : 0, mutationId ?? "");
            InsertParts(id, message.OptJArray("parts"));
        }

        private void InsertParts(string messageId, JArray parts)
        {
            for (var i = 0; parts != null && i < parts.Length(); i++)
            {
                var part = parts.OptJObject(i);
                if (part == null) continue;
                _db.Execute("INSERT OR REPLACE INTO message_part VALUES (?,?,?,?,?,?,?)", messageId, i,
                    part.OptString("kind", "text"), part.OptString("text"), part.OptString("media_id"),
                    part.OptString("sticker"), part.OptString("url"));
            }
        }

        private ChatItem FindByMutation(string mutationId)
        {
            var mutation = Mutation(mutationId);
            if (mutation == null) return null;
            var accepted = mutation.State == "accepted"
                ? new JObject(_db.Query("SELECT result_json FROM chat_outbox WHERE client_mutation_id=?", mutationId)[0]
                    .String("result_json")).OptJObject("message") : null;
            var id = accepted?.OptString("id") ?? ("local_" + mutationId);
            var rows = _db.Query("SELECT local_seq,payload,pending,mutation_id FROM message WHERE id=?", id);
            var items = ReadItems(rows);
            return items.Count == 0 ? null : items[0];
        }

        private static List<ChatItem> ReadItems(List<SqliteDb.Row> rows)
        {
            var result = new List<ChatItem>();
            foreach (var row in rows)
            {
                var pending = row.Long("pending") != 0;
                var mutation = row.String("mutation_id");
                var json = new JObject(row.String("payload"));
                if (pending) json.Put("pending", true).Put("client_mutation_id", mutation);
                result.Add(new ChatItem { Json = json, LocalSeq = row.Long("local_seq"),
                    Pending = pending, MutationId = mutation });
            }
            return result;
        }

        private static ChatMutation ReadMutation(SqliteDb.Row row)
        {
            return new ChatMutation { Id = row.String("client_mutation_id"),
                ConversationId = row.String("conversation_id"), Parts = new JArray(row.String("parts")),
                State = row.String("state"), Attempts = (int)row.Long("attempts"),
                Error = row.String("last_error"), UpdatedAt = row.Double("updated_at") };
        }

        private static double Now() { return DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0; }
    }
}
