using System;
using System.Collections.Generic;

namespace BlueAgent.Logic
{
    /// <summary>Repository 回給 UI 的本地帖子／回覆；Pending 與 mutation id 不是服務端欄位。</summary>
    public sealed class SocialItem
    {
        public JObject Json { get; internal set; }
        public bool Pending { get; internal set; }
        public string MutationId { get; internal set; } = "";
        public string Error { get; internal set; } = "";
    }

    /// <summary>持久本地寫前日誌；Accepted 也保留結果作為重啟後的證據。</summary>
    public sealed class SocialMutation
    {
        public string Id { get; internal set; } = "";
        public string Kind { get; internal set; } = "";
        public JObject Payload { get; internal set; }
        public string State { get; internal set; } = "";
        public int Attempts { get; internal set; }
        public string Error { get; internal set; } = "";
        public JObject Result { get; internal set; }
        public double UpdatedAt { get; internal set; }

        public bool Retryable { get { return State == "pending"; } }
    }

    /// <summary>
    /// S2 公共區本地真副本：SQLite 保存帖子、回覆、反應、游標與 mutation outbox。
    /// Enqueue 在同一交易裡更新本地投影＋Outbox；網路回包與事件也走同一個 Upsert 入口。
    /// </summary>
    public sealed class SocialRepository : IDisposable
    {
        public sealed class Author
        {
            public string Id = "";
            public string Kind = "owner";
            public string Display = "";

            public Author(string id, string kind, string display)
            {
                Id = id ?? "";
                Kind = string.IsNullOrEmpty(kind) ? "owner" : kind;
                Display = display ?? "";
            }
        }

        private const string Schema = @"
CREATE TABLE IF NOT EXISTS post(
  id TEXT PRIMARY KEY, at REAL NOT NULL, payload TEXT NOT NULL,
  pending INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
  mutation_id TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS post_feed ON post(deleted, at DESC, id DESC);
CREATE TABLE IF NOT EXISTS reply(
  id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, at REAL NOT NULL, payload TEXT NOT NULL,
  pending INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
  mutation_id TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS reply_parent ON reply(parent_id, deleted, at, id);
CREATE TABLE IF NOT EXISTS reaction(
  post_id TEXT NOT NULL, actor_id TEXT NOT NULL, kind TEXT NOT NULL,
  pending INTEGER NOT NULL DEFAULT 1, mutation_id TEXT NOT NULL,
  PRIMARY KEY(post_id, actor_id, kind));
CREATE TABLE IF NOT EXISTS mutation_outbox(
  client_mutation_id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
  state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  result_json TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS mutation_ready ON mutation_outbox(state, created_at, client_mutation_id);
CREATE TABLE IF NOT EXISTS sync_cursor(scope TEXT PRIMARY KEY, cursor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS applied_event(event_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS mood_state(
  account_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL);";

        private readonly SqliteDb _db;

        public SocialRepository(string path)
        {
            _db = new SqliteDb(path);
            foreach (var statement in Schema.Split(';'))
                if (!string.IsNullOrWhiteSpace(statement)) _db.Execute(statement);
        }
        public void Dispose() { _db.Dispose(); }

        public List<SocialItem> Feed(int limit = 100)
        {
            return Read(_db.Query(
                "SELECT payload,pending,mutation_id FROM post WHERE deleted=0 " +
                "ORDER BY at DESC,id DESC LIMIT " + Math.Max(1, Math.Min(500, limit))));
        }

        public List<SocialItem> Replies(string parentId)
        {
            return Read(_db.Query(
                "SELECT payload,pending,mutation_id FROM reply WHERE parent_id=? AND deleted=0 ORDER BY at,id",
                parentId ?? ""));
        }

        public SocialItem Item(string id, bool reply = false)
        {
            var table = reply ? "reply" : "post";
            var rows = _db.Query("SELECT payload,pending,mutation_id FROM " + table + " WHERE id=?", id ?? "");
            var items = Read(rows);
            return items.Count == 0 ? null : items[0];
        }

        public void MergeFeed(JArray posts)
        {
            _db.Transaction(() =>
            {
                for (var i = 0; posts != null && i < posts.Length(); i++)
                {
                    var post = posts.OptJObject(i);
                    if (post != null) Upsert("post", post, false);
                }
            });
        }

        public void MergeReplies(string parentId, JArray replies)
        {
            _db.Transaction(() =>
            {
                for (var i = 0; replies != null && i < replies.Length(); i++)
                {
                    var reply = replies.OptJObject(i);
                    if (reply == null) continue;
                    if (!reply.Has("parent_id")) reply.Put("parent_id", parentId ?? "");
                    Upsert("reply", reply, false);
                }
            });
        }

        public SocialItem EnqueuePost(string text, JArray media, Author author, double at, string mutationId)
        {
            SocialItem result = null;
            _db.Transaction(() =>
            {
                var existing = ExistingItem(mutationId, "post");
                if (existing != null) { result = existing; return; }
                RequireMutation(mutationId, author);
                var localId = "local_" + mutationId;
                var payload = NewPost(localId, text, media, author, at, "", "post");
                Insert("post", payload, mutationId);
                InsertMutation(mutationId, "post.create", MutationPayload(text, media, ""), at);
                result = new SocialItem { Json = payload, Pending = true, MutationId = mutationId };
            });
            return result;
        }

        public SocialItem EnqueueReply(string parentId, string text, JArray media, Author author,
                                       double at, string mutationId)
        {
            if (string.IsNullOrEmpty(parentId)) throw new ArgumentException("parent id is empty");
            SocialItem result = null;
            _db.Transaction(() =>
            {
                var existing = ExistingItem(mutationId, "reply");
                if (existing != null) { result = existing; return; }
                RequireMutation(mutationId, author);
                var localId = "local_" + mutationId;
                var payload = NewPost(localId, text, media, author, at, parentId, "reply");
                Insert("reply", payload, mutationId);
                InsertMutation(mutationId, "reply.create", MutationPayload(text, media, parentId), at);
                result = new SocialItem { Json = payload, Pending = true, MutationId = mutationId };
            });
            return result;
        }

        /// <summary>
        /// 離線選的圖先以本機 id 佔位（`pending-…`，檔案在端末自己的 outbox 資料夾；2026-11-17 媒體缺口 S2）：
        /// 投稿／回覆照常入佇列，Outbox 送出前才上傳、拿到伺服器 id 後用這支換掉佔位。
        /// </summary>
        public const string PendingMediaPrefix = "pending-";

        public static bool IsPendingMedia(string id)
        {
            return !string.IsNullOrEmpty(id) && id.StartsWith(PendingMediaPrefix, StringComparison.Ordinal);
        }

        /// <summary>
        /// 把一筆待送投稿／回覆裡的佔位圖換成伺服器 id（`remoteId` 空＝拿掉這張，例如本機檔已不見）。
        /// 佇列裡的 payload 與畫面上那則本機帖一起改（同一交易），重啟後從換好的狀態繼續。
        /// </summary>
        public void ResolveMedia(string mutationId, string pendingId, string remoteId)
        {
            _db.Transaction(() =>
            {
                var mutation = Mutation(mutationId);
                if (mutation == null || mutation.State == "accepted") return;
                var payload = mutation.Payload;
                payload.Put("media", SwapMedia(payload.OptJArray("media"), pendingId, remoteId));
                _db.Execute("UPDATE mutation_outbox SET payload=? WHERE client_mutation_id=?",
                    payload.ToString(), mutationId);
                var reply = mutation.Kind == "reply.create";
                var local = Item("local_" + mutationId, reply);
                if (local == null) return;
                var json = local.Json;
                json.Put("media", SwapMedia(json.OptJArray("media"), pendingId, remoteId));
                _db.Execute("UPDATE " + (reply ? "reply" : "post") + " SET payload=? WHERE id=?",
                    json.ToString(), "local_" + mutationId);
            });
        }

        private static JArray SwapMedia(JArray media, string pendingId, string remoteId)
        {
            var result = new JArray();
            if (media == null) return result;
            for (var i = 0; i < media.Length(); i++)
            {
                var id = media.OptString(i);
                if (id == pendingId) id = remoteId ?? "";
                if (!string.IsNullOrEmpty(id)) result.Put(id);
            }
            return result;
        }

        public string EnqueueLike(string postId, Author author, double at, string mutationId)
        {
            RequireMutation(mutationId, author);
            _db.Transaction(() =>
            {
                if (Mutation(mutationId) != null) return;
                var post = Item(postId);
                if (post == null) throw new InvalidOperationException("unknown_local_post");
                var json = post.Json;
                var previousLiked = json.OptBoolean("liked");
                var previousLikes = json.OptInt("likes");
                if (!previousLiked) json.Put("liked", true).Put("likes", previousLikes + 1);
                _db.Execute("UPDATE post SET payload=?,pending=1,mutation_id=? WHERE id=?",
                    json.ToString(), mutationId, postId);
                _db.Execute("INSERT OR REPLACE INTO reaction VALUES (?,?,?,1,?)",
                    postId, author.Id, "like", mutationId);
                InsertMutation(mutationId, "post.like", new JObject().Put("id", postId)
                    .Put("previous_liked", previousLiked).Put("previous_likes", previousLikes), at);
            });
            return mutationId;
        }

        public string EnqueueDelete(string postId, bool reply, double at, string mutationId)
        {
            if (string.IsNullOrEmpty(mutationId)) throw new ArgumentException("mutation id is empty");
            _db.Transaction(() =>
            {
                if (Mutation(mutationId) != null) return;
                var table = reply ? "reply" : "post";
                _db.Execute("UPDATE " + table + " SET deleted=1,pending=1,mutation_id=? WHERE id=?",
                    mutationId, postId ?? "");
                InsertMutation(mutationId, reply ? "reply.delete" : "post.delete",
                    new JObject().Put("id", postId ?? ""), at);
            });
            return mutationId;
        }

        public SocialMutation Mutation(string id)
        {
            var rows = _db.Query("SELECT * FROM mutation_outbox WHERE client_mutation_id=?", id ?? "");
            return rows.Count == 0 ? null : ReadMutation(rows[0]);
        }

        public List<SocialMutation> PendingMutations()
        {
            var rows = _db.Query("SELECT * FROM mutation_outbox WHERE state='pending' " +
                                 "ORDER BY created_at,client_mutation_id LIMIT 200");
            var result = new List<SocialMutation>();
            foreach (var row in rows) result.Add(ReadMutation(row));
            return result;
        }

        public List<SocialMutation> AllMutations()
        {
            var rows = _db.Query("SELECT * FROM mutation_outbox ORDER BY created_at,client_mutation_id");
            var result = new List<SocialMutation>();
            foreach (var row in rows) result.Add(ReadMutation(row));
            return result;
        }

        public void MarkAttempt(string id, double at)
        {
            _db.Execute("UPDATE mutation_outbox SET attempts=attempts+1,updated_at=? " +
                        "WHERE client_mutation_id=? AND state='pending'", at, id ?? "");
        }

        public void MarkAccepted(string id, JObject response, double at)
        {
            _db.Transaction(() =>
            {
                var mutation = Mutation(id);
                if (mutation == null || mutation.State == "accepted") return;
                var result = response ?? new JObject();
                _db.Execute("UPDATE mutation_outbox SET state='accepted',result_json=?,last_error='',updated_at=? " +
                            "WHERE client_mutation_id=?", result.ToString(), at, id);
                if (mutation.Kind == "post.create" || mutation.Kind == "reply.create")
                    AcceptCreated(mutation, result.OptJObject("post"));
                else if (mutation.Kind == "post.like")
                    AcceptLike(mutation, result.OptJObject("post"));
                else
                    AcceptDelete(mutation);
            });
        }

        public void MarkFailed(string id, string error, bool retry, double at)
        {
            _db.Transaction(() =>
            {
                var mutation = Mutation(id);
                if (mutation == null || mutation.State == "accepted") return;
                _db.Execute("UPDATE mutation_outbox SET state=?,last_error=?,updated_at=? WHERE client_mutation_id=?",
                    retry ? "pending" : "failed", error ?? "unknown", at, id);
                if (!retry && mutation.Kind == "post.like")
                {
                    var postId = mutation.Payload.OptString("id");
                    var current = Item(postId);
                    if (current != null)
                    {
                        current.Json.Put("liked", mutation.Payload.OptBoolean("previous_liked"))
                            .Put("likes", mutation.Payload.OptInt("previous_likes"));
                        current.Json.Remove("pending");
                        current.Json.Remove("client_mutation_id");
                        _db.Execute(@"UPDATE post SET payload=?,
                            pending=CASE WHEN mutation_id=? THEN 0 ELSE pending END,
                            mutation_id=CASE WHEN mutation_id=? THEN '' ELSE mutation_id END WHERE id=?",
                            current.Json.ToString(), id, id, postId);
                    }
                    _db.Execute("DELETE FROM reaction WHERE mutation_id=?", id);
                }
                else if (!retry && (mutation.Kind == "post.delete" || mutation.Kind == "reply.delete"))
                {
                    var table = mutation.Kind == "reply.delete" ? "reply" : "post";
                    _db.Execute("UPDATE " + table + " SET deleted=0,pending=0,mutation_id='' WHERE id=?",
                        mutation.Payload.OptString("id"));
                }
            });
        }

        public void RetryFailed(double at)
        {
            _db.Execute("UPDATE mutation_outbox SET state='pending',last_error='',updated_at=? WHERE state='failed'", at);
        }

        public JObject Mood(string accountId)
        {
            var rows = _db.Query("SELECT payload FROM mood_state WHERE account_id=?", accountId ?? "");
            return rows.Count == 0 ? null : new JObject(rows[0].String("payload"));
        }

        public List<JObject> Moods()
        {
            var rows = _db.Query("SELECT payload FROM mood_state ORDER BY account_id");
            var result = new List<JObject>();
            foreach (var row in rows) result.Add(new JObject(row.String("payload")));
            return result;
        }

        public void MergeMoods(JArray moods)
        {
            _db.Transaction(() =>
            {
                for (var i = 0; moods != null && i < moods.Length(); i++) MergeMood(moods.OptJObject(i));
            });
        }

        /// <summary>權威可見域快照；呼叫端必須擋掉與事件競爭的舊回包。</summary>
        public void ReplaceMoods(JArray moods)
        {
            _db.Transaction(() =>
            {
                _db.Execute("DELETE FROM mood_state");
                for (var i = 0; moods != null && i < moods.Length(); i++) MergeMood(moods.OptJObject(i));
            });
        }

        public void MergeMood(JObject mood)
        {
            var projected = PublicMood(mood);
            if (projected == null) return;
            _db.Execute(@"INSERT INTO mood_state VALUES (?,?,?)
                ON CONFLICT(account_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at
                WHERE excluded.updated_at > mood_state.updated_at",
                projected.OptString("account_id"), projected.ToString(), projected.OptDouble("updated_at"));
        }

        public void DeleteMood(string accountId)
        {
            _db.Execute("DELETE FROM mood_state WHERE account_id=?", accountId ?? "");
        }

        private static JObject PublicMood(JObject mood)
        {
            if (mood == null || string.IsNullOrEmpty(mood.OptString("account_id")) ||
                string.IsNullOrEmpty(mood.OptString("key"))) return null;
            // 只保存公開契約欄位；即使舊資料或異常回包帶了私有數值，也不落進平台副本。
            return new JObject().Put("account_id", mood.OptString("account_id"))
                .Put("kind", mood.OptString("kind")).Put("display", mood.OptString("display"))
                .Put("key", mood.OptString("key")).Put("note", mood.OptString("note"))
                .Put("updated_at", mood.OptDouble("updated_at")).Put("event_id", mood.OptString("event_id"));
        }

        public string Cursor(string scope)
        {
            var rows = _db.Query("SELECT cursor FROM sync_cursor WHERE scope=?", scope ?? "feed");
            return rows.Count == 0 ? "" : rows[0].String("cursor");
        }

        public void SetCursor(string scope, string cursor)
        {
            _db.Execute("INSERT INTO sync_cursor VALUES (?,?) ON CONFLICT(scope) DO UPDATE SET cursor=excluded.cursor",
                scope ?? "feed", cursor ?? "");
        }

        /// <summary>事件先記 ID 再改投影；同一幀重放不會重複改變本地狀態。</summary>
        public void ApplyEvent(JObject envelope)
        {
            if (envelope == null) return;
            var id = envelope.OptString("event_id");
            if (string.IsNullOrEmpty(id)) throw new ArgumentException("event id is empty");
            _db.Transaction(() =>
            {
                _db.Execute("INSERT OR IGNORE INTO applied_event VALUES (?)", id);
                if (_db.ScalarLong("SELECT changes() AS value") == 0) return;
                var type = envelope.OptString("type");
                var payload = envelope.OptJObject("payload");
                var post = payload == null ? null : payload.OptJObject("post");
                if (type == "post.created" && post != null) Upsert("post", post, false);
                else if (type == "reply.created" && post != null) Upsert("reply", post, false);
                else if (type == "mood.updated")
                {
                    var mood = payload == null ? null : payload.OptJObject("mood");
                    if (mood != null) MergeMood(mood);
                }
                else if (type == "post.deleted")
                {
                    var postId = payload == null ? "" : payload.OptString("post_id");
                    _db.Execute("UPDATE post SET deleted=1 WHERE id=?", postId);
                    _db.Execute("UPDATE reply SET deleted=1 WHERE id=?", postId);
                }
                // post.liked 只有 post_id、沒有權威計數；記住事件，下一次 feed 快照再對帳，不能自行加兩次。
            });
        }

        private static List<SocialItem> Read(List<SqliteDb.Row> rows)
        {
            var result = new List<SocialItem>();
            foreach (var row in rows)
            {
                var pending = row.Long("pending") != 0;
                var mutationId = row.String("mutation_id");
                var json = new JObject(row.String("payload"));
                if (pending) json.Put("pending", true).Put("client_mutation_id", mutationId);
                result.Add(new SocialItem
                {
                    Json = json, Pending = pending, MutationId = mutationId,
                });
            }
            return result;
        }

        private SocialItem ExistingItem(string mutationId, string table)
        {
            if (string.IsNullOrEmpty(mutationId) || Mutation(mutationId) == null) return null;
            var accepted = Mutation(mutationId);
            var serverId = accepted.State == "accepted"
                ? accepted.Result?.OptJObject("post")?.OptString("id") : "";
            return Item(string.IsNullOrEmpty(serverId) ? "local_" + mutationId : serverId, table == "reply");
        }

        private static void RequireMutation(string id, Author author)
        {
            if (string.IsNullOrEmpty(id)) throw new ArgumentException("mutation id is empty");
            if (author == null || string.IsNullOrEmpty(author.Id)) throw new ArgumentException("author id is empty");
        }

        private static JObject NewPost(string id, string text, JArray media, Author author,
                                       double at, string parentId, string kind)
        {
            return new JObject().Put("id", id).Put("at", at).Put("author_id", author.Id)
                .Put("author_kind", author.Kind).Put("display", author.Display)
                .Put("body", text ?? "").Put("media", media ?? new JArray())
                .Put("kind", kind).Put("parent_id", parentId ?? "").Put("origin", "local")
                .Put("likes", 0).Put("replies", 0).Put("liked", false);
        }

        private static JObject MutationPayload(string text, JArray media, string parentId)
        {
            var payload = new JObject().Put("text", text ?? "").Put("media", media ?? new JArray());
            if (!string.IsNullOrEmpty(parentId)) payload.Put("id", parentId);
            return payload;
        }

        private void Insert(string table, JObject payload, string mutationId)
        {
            if (table == "reply")
                _db.Execute("INSERT INTO reply VALUES (?,?,?,?,1,0,?)", payload.OptString("id"),
                    payload.OptString("parent_id"), payload.OptDouble("at"), payload.ToString(), mutationId);
            else
                _db.Execute("INSERT INTO post VALUES (?,?,?,1,0,?)", payload.OptString("id"),
                    payload.OptDouble("at"), payload.ToString(), mutationId);
        }

        private void Upsert(string table, JObject payload, bool pending)
        {
            var id = payload.OptString("id");
            if (string.IsNullOrEmpty(id)) return;
            if (table == "reply")
                _db.Execute(@"INSERT INTO reply VALUES (?,?,?,?,?,0,'')
                    ON CONFLICT(id) DO UPDATE SET at=excluded.at,payload=excluded.payload,
                      pending=excluded.pending,deleted=0,mutation_id='' WHERE reply.pending=0",
                    id, payload.OptString("parent_id"), payload.OptDouble("at"), payload.ToString(), pending ? 1 : 0);
            else
                _db.Execute(@"INSERT INTO post VALUES (?,?,?,?,0,'')
                    ON CONFLICT(id) DO UPDATE SET at=excluded.at,payload=excluded.payload,
                      pending=excluded.pending,deleted=0,mutation_id='' WHERE post.pending=0",
                    id, payload.OptDouble("at"), payload.ToString(), pending ? 1 : 0);
        }

        private void InsertMutation(string id, string kind, JObject payload, double at)
        {
            _db.Execute("INSERT INTO mutation_outbox(client_mutation_id,kind,payload,state,created_at,updated_at) " +
                        "VALUES (?,?,?,'pending',?,?)", id, kind, payload.ToString(), at, at);
        }

        private void AcceptCreated(SocialMutation mutation, JObject post)
        {
            var table = mutation.Kind == "reply.create" ? "reply" : "post";
            var localId = "local_" + mutation.Id;
            var local = Item(localId, table == "reply");
            if (post == null) throw new InvalidOperationException("accepted response has no post");
            var carryMutation = "";
            var carryDeleted = false;
            if (local != null)
            {
                var serverId = post.OptString("id");
                // 本地可能已排入後續按讚／刪除；建立回包只能確認自己，不能抹掉後續 mutation 的狀態。
                if (local.MutationId != mutation.Id)
                {
                    carryMutation = local.MutationId;
                    carryDeleted = _db.ScalarLong("SELECT deleted AS value FROM " + table + " WHERE id=?", localId) != 0;
                }
                RemapLocalId(localId, serverId);
                _db.Execute("DELETE FROM " + table + " WHERE id=?", localId);
            }
            Upsert(table, post, false);
            if (!string.IsNullOrEmpty(carryMutation))
                _db.Execute("UPDATE " + table + " SET pending=1,deleted=?,mutation_id=? WHERE id=?",
                    carryDeleted ? 1 : 0, carryMutation, post.OptString("id"));
        }

        private void RemapLocalId(string localId, string serverId)
        {
            if (string.IsNullOrEmpty(localId) || string.IsNullOrEmpty(serverId) || localId == serverId) return;
            foreach (var pending in PendingMutations())
            {
                if (pending.Payload.OptString("id") != localId) continue;
                pending.Payload.Put("id", serverId);
                _db.Execute("UPDATE mutation_outbox SET payload=? WHERE client_mutation_id=?",
                    pending.Payload.ToString(), pending.Id);
            }
            _db.Execute("UPDATE reaction SET post_id=? WHERE post_id=?", serverId, localId);
            var replies = _db.Query("SELECT id,payload FROM reply WHERE parent_id=?", localId);
            foreach (var row in replies)
            {
                var payload = new JObject(row.String("payload")).Put("parent_id", serverId);
                _db.Execute("UPDATE reply SET parent_id=?,payload=? WHERE id=?",
                    serverId, payload.ToString(), row.String("id"));
            }
        }

        private void AcceptLike(SocialMutation mutation, JObject post)
        {
            var id = mutation.Payload.OptString("id");
            _db.Execute("UPDATE reaction SET pending=0 WHERE mutation_id=?", mutation.Id);
            // 這是已確認 mutation 的權威回包，必須越過 Upsert 對普通 feed 快照的 pending 保護；
            // 但同一帖子後面若已排入刪除，不能把那一筆的 pending 身份清掉。
            if (post != null)
                _db.Execute(@"UPDATE post SET payload=?,
                    pending=CASE WHEN mutation_id=? THEN 0 ELSE pending END,
                    mutation_id=CASE WHEN mutation_id=? THEN '' ELSE mutation_id END WHERE id=?",
                    post.ToString(), mutation.Id, mutation.Id, id);
            else
                _db.Execute(@"UPDATE post SET
                    pending=CASE WHEN mutation_id=? THEN 0 ELSE pending END,
                    mutation_id=CASE WHEN mutation_id=? THEN '' ELSE mutation_id END WHERE id=?",
                    mutation.Id, mutation.Id, id);
        }

        private void AcceptDelete(SocialMutation mutation)
        {
            var table = mutation.Kind == "reply.delete" ? "reply" : "post";
            _db.Execute("UPDATE " + table + " SET pending=0,mutation_id='' WHERE id=?",
                mutation.Payload.OptString("id"));
        }

        private static SocialMutation ReadMutation(SqliteDb.Row row)
        {
            var result = row.String("result_json");
            return new SocialMutation
            {
                Id = row.String("client_mutation_id"), Kind = row.String("kind"),
                Payload = new JObject(row.String("payload")), State = row.String("state"),
                Attempts = (int)row.Long("attempts"), Error = row.String("last_error"),
                Result = string.IsNullOrEmpty(result) ? null : new JObject(result),
                UpdatedAt = row.Double("updated_at"),
            };
        }
    }
}
