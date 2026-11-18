using System;
using System.IO;
using System.Linq;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>S3 真 SQLite：離線寫、重開、回包／事件重放與亂序合併。</summary>
    public sealed class ChatRepositoryTest
    {
        private string _root;
        private string _path;

        [SetUp]
        public void SetUp()
        {
            _root = Path.Combine(Path.GetTempPath(), "ba-chat-" + Guid.NewGuid().ToString("N"));
            _path = Path.Combine(_root, "owner", "chat.db");
        }

        [TearDown]
        public void TearDown() { if (Directory.Exists(_root)) Directory.Delete(_root, true); }

        private ChatRepository Open() { return new ChatRepository(_path); }

        private static JArray Parts(string text)
        {
            return new JArray().Put(new JObject().Put("kind", "text").Put("text", text));
        }

        private static JObject ServerMessage(string id, long seq, string text, string author = "aster")
        {
            return new JObject().Put("id", id).Put("seq", seq)
                .Put("conversation_id", ChatRepository.DirectId("owner", "aster"))
                .Put("created_at", 1000.0 + seq)
                .Put("author", new JObject().Put("id", author).Put("kind", author == "owner" ? "owner" : "entity"))
                .Put("parts", Parts(text));
        }

        private static JObject GroupConversation()
        {
            return new JObject().Put("id", "agent-room").Put("kind", "group")
                .Put("name", "Agent Room").Put("created_at", 1000.0)
                .Put("participants", JArray.OfStrings(new[] { "acct_owner", "aster", "nori" }));
        }

        private static JObject GroupMessage(string id, long seq, string text, string author)
        {
            return new JObject().Put("id", id).Put("seq", seq)
                .Put("conversation_id", "agent-room").Put("created_at", 1000.0 + seq)
                .Put("author", new JObject().Put("id", author).Put("kind", "entity"))
                .Put("parts", Parts(text));
        }

        private static JObject Created(string eventId, JObject message)
        {
            return new JObject().Put("event_id", eventId).Put("type", "chat.message.created")
                .Put("payload", new JObject().Put("message", message));
        }

        [Test]
        public void GroupUnreadCountsOthersOncePerEventAndClearsOnRead()
        {
            using (var db = Open())
            {
                db.MergeConversation(GroupConversation(), "acct_owner");
                var first = GroupMessage("msg_g1", 1, "おはよう", "aster");
                db.ApplyEvent(Created("evt_g1", first), "acct_owner");
                db.ApplyEvent(Created("evt_g1", first), "acct_owner");            // 重放不重算
                db.ApplyEvent(Created("evt_g2", GroupMessage("msg_g2", 2, "ん", "nori")), "acct_owner");
                db.ApplyEvent(Created("evt_g3", GroupMessage("msg_g3", 3, "私だ", "acct_owner")), "acct_owner");
                Assert.That(db.GroupUnread(), Is.EqualTo(2), "自分の発言は未読にしない");
                Assert.That(db.ConversationById("agent-room").OptJObject("last_message").OptString("id"),
                    Is.EqualTo("msg_g3"));
                db.MarkRead("agent-room", "msg_g3");
                Assert.That(db.GroupUnread(), Is.EqualTo(0));
                db.ApplyEvent(Created("evt_g4", GroupMessage("msg_g4", 4, "ねえ", "aster")));   // reader 未指定
                Assert.That(db.GroupUnread(), Is.EqualTo(0), "呼び手が自分を名乗らないときは数えない");
            }
        }

        private static JObject Tombstone(string id, long seq, string author = "owner")
        {
            return ServerMessage(id, seq, "", author).Put("deleted", true).Put("deleted_at", 2000.0)
                .Put("parts", new JArray());
        }

        [Test]
        public void RecalledMessageBecomesTombstoneOnceAndDoesNotRewindLastMessage()
        {
            using (var db = Open())
            {
                db.EnsureConversation("aster", "owner", "主人");
                db.MergeMessages(new JArray().Put(ServerMessage("msg_1", 1, "消す", "owner"))
                    .Put(ServerMessage("msg_2", 2, "残る")));
                var deleted = new JObject().Put("event_id", "evt_del").Put("type", "chat.message.deleted")
                    .Put("payload", new JObject().Put("message", Tombstone("msg_1", 1)));
                db.ApplyEvent(deleted, "owner");
                db.ApplyEvent(deleted, "owner");          // 重放：event_id 去重
                var rows = db.Messages("aster");
                Assert.That(rows.Select(row => row.Json.OptString("id")), Is.EqualTo(new[] { "msg_1", "msg_2" }));
                Assert.That(rows[0].Json.OptBoolean("deleted"), Is.True);
                Assert.That(rows[0].Json.OptJArray("parts").Length(), Is.EqualTo(0));
                Assert.That(db.Conversation("aster").OptJObject("last_message").OptString("id"), Is.EqualTo("msg_2"),
                    "收回舊的那一則，最後一則不能倒退成它");
                db.ApplyTombstone(Tombstone("msg_2", 2, "aster"));
                Assert.That(db.Conversation("aster").OptJObject("last_message").OptBoolean("deleted"), Is.True);
                db.ApplyTombstone(Tombstone("msg_unknown", 9));      // 本地沒有就不新增
                Assert.That(db.Messages("aster").Count, Is.EqualTo(2));
            }
        }

        [Test]
        public void DirectIdMatchesServerStablePairing()
        {
            Assert.That(ChatRepository.DirectId("owner", "aster"),
                Is.EqualTo(ChatRepository.DirectId("aster", "owner")));
            Assert.That(ChatRepository.DirectId("owner", "aster"), Does.StartWith("cnv_"));
            Assert.That(ChatRepository.DirectId("owner", "aster").Length, Is.EqualTo(28));
        }

        [Test]
        public void OfflineMessageAndPartsSurviveReopen()
        {
            using (var db = Open())
            {
                var item = db.Enqueue("aster", "owner", "主人", Parts("離線訊息"), "cm_1", 1000);
                Assert.That(item.Pending, Is.True);
                Assert.That(db.Pending().Single().Parts.OptJObject(0).OptString("text"), Is.EqualTo("離線訊息"));
            }
            using (var db = Open())
            {
                var message = db.Messages("aster").Single();
                Assert.That(message.Json.OptJArray("parts").OptJObject(0).OptString("text"), Is.EqualTo("離線訊息"));
                Assert.That(message.Pending, Is.True);
                Assert.That(db.Pending().Single().Id, Is.EqualTo("cm_1"));
            }
        }

        [Test]
        public void SameMutationNeverCreatesSecondMessage()
        {
            using (var db = Open())
            {
                db.Enqueue("aster", "owner", "主人", Parts("一次"), "cm_same", 1000);
                db.Enqueue("aster", "owner", "主人", Parts("改過也同一次"), "cm_same", 1001);
                Assert.That(db.Messages("aster").Count, Is.EqualTo(1));
                Assert.That(db.Messages("aster")[0].Json.OptJArray("parts").OptJObject(0).OptString("text"),
                    Is.EqualTo("一次"));
            }
        }

        [Test]
        public void AcceptedResponseThenEventDoesNotDuplicate()
        {
            using (var db = Open())
            {
                db.Enqueue("aster", "owner", "主人", Parts("待確認"), "cm_accept", 1000);
                var server = ServerMessage("msg_server", 9, "待確認", "owner");
                db.MarkAccepted("cm_accept", new JObject().Put("message", server), 1002);
                var envelope = new JObject().Put("event_id", "evt_1").Put("type", "chat.message.created")
                    .Put("payload", new JObject().Put("message", server));
                db.ApplyEvent(envelope);
                db.ApplyEvent(envelope);
                Assert.That(db.Messages("aster").Count, Is.EqualTo(1));
                Assert.That(db.Messages("aster")[0].Json.OptString("id"), Is.EqualTo("msg_server"));
                Assert.That(db.Pending(), Is.Empty);
            }
            using (var db = Open()) { Assert.That(db.Messages("aster").Count, Is.EqualTo(1)); }
        }

        [Test]
        public void EventBeforeResponseAlsoConverges()
        {
            using (var db = Open())
            {
                db.Enqueue("aster", "owner", "主人", Parts("亂序"), "cm_race", 1000);
                var server = ServerMessage("msg_race", 10, "亂序", "owner");
                db.ApplyEvent(new JObject().Put("event_id", "evt_race").Put("type", "chat.message.created")
                    .Put("payload", new JObject().Put("message", server)));
                db.MarkAccepted("cm_race", new JObject().Put("message", server), 1002);
                Assert.That(db.Messages("aster").Select(row => row.Json.OptString("id")),
                    Is.EqualTo(new[] { "msg_race" }));
            }
        }

        [Test]
        public void OlderPagingAndCursorPersist()
        {
            using (var db = Open())
            {
                db.EnsureConversation("aster", "owner");
                db.MergeMessages(new JArray().Put(ServerMessage("msg_1", 1, "一"))
                    .Put(ServerMessage("msg_2", 2, "二")).Put(ServerMessage("msg_3", 3, "三")));
                var latest = db.Messages("aster", 2);
                Assert.That(latest.Select(row => row.Json.OptString("id")), Is.EqualTo(new[] { "msg_2", "msg_3" }));
                var older = db.Messages("aster", 2, latest[0].Json.OptString("id"));
                Assert.That(older.Single().Json.OptString("id"), Is.EqualTo("msg_1"));
                db.SetCursor(ChatRepository.DirectId("owner", "aster"), "3");
            }
            using (var db = Open())
            {
                Assert.That(db.Cursor(ChatRepository.DirectId("owner", "aster")), Is.EqualTo("3"));
            }
        }

        [Test]
        public void LatestWindowFollowsMessageTimeWhenHistoryIsBackfilledLater()
        {
            using (var db = Open())
            {
                db.EnsureConversation("aster", "owner");
                // 先收到最新兩條（即時事件），之後才補拉更早的歷史 ⇒ 舊訊息的本地插入序反而較大。
                db.MergeMessages(new JArray().Put(ServerMessage("msg_9", 9, "九"))
                    .Put(ServerMessage("msg_10", 10, "十")));
                db.MergeMessages(new JArray().Put(ServerMessage("msg_1", 1, "一"))
                    .Put(ServerMessage("msg_2", 2, "二")).Put(ServerMessage("msg_3", 3, "三")));
                var latest = db.Messages("aster", 2);
                Assert.That(latest.Select(row => row.Json.OptString("id")), Is.EqualTo(new[] { "msg_9", "msg_10" }));
                var older = db.Messages("aster", 2, latest[0].Json.OptString("id"));
                Assert.That(older.Select(row => row.Json.OptString("id")), Is.EqualTo(new[] { "msg_2", "msg_3" }));
                var oldest = db.Messages("aster", 2, older[0].Json.OptString("id"));
                Assert.That(oldest.Select(row => row.Json.OptString("id")), Is.EqualTo(new[] { "msg_1" }));
                // 本機剛送、尚未確認的那條仍在最後。
                db.Enqueue("aster", "owner", "主人", Parts("剛送"), "cm_tail", 2000);
                Assert.That(db.Messages("aster", 2).Last().Pending, Is.True);
            }
        }

        [Test]
        public void ReadPositionSurvivesReopenAndWinsOverStaleConversationUnread()
        {
            var id = ChatRepository.DirectId("owner", "aster");
            using (var db = Open())
            {
                db.EnsureConversation("aster", "owner");
                db.MergeMessages(new JArray().Put(ServerMessage("msg_read", 4, "已讀")));
                db.MarkRead(id, "msg_read");
                Assert.That(db.Conversation("aster").OptInt("unread"), Is.EqualTo(0));
                var stale = db.Conversation("aster").Put("unread", 3);
                db.MergeConversation(stale, "owner");
                Assert.That(db.Conversation("aster").OptInt("unread"), Is.EqualTo(0));
            }
            using (var db = Open())
            {
                Assert.That(db.ReadPosition(id), Is.EqualTo("msg_read"));
                Assert.That(db.Conversation("aster").OptInt("unread"), Is.EqualTo(0));
            }
        }

        [Test]
        public void NewMessageAfterReadPositionCanBeUnreadAgain()
        {
            var id = ChatRepository.DirectId("owner", "aster");
            using (var db = Open())
            {
                db.EnsureConversation("aster", "owner");
                db.MergeMessages(new JArray().Put(ServerMessage("msg_before", 1, "之前")));
                db.MarkRead(id, "msg_before");
                db.MergeMessages(new JArray().Put(ServerMessage("msg_new", 2, "新消息")));
                var summary = db.Conversation("aster").Put("unread", 1);
                db.MergeConversation(summary, "owner");
                Assert.That(db.Conversation("aster").OptInt("unread"), Is.EqualTo(1));
            }
        }

        [Test]
        public void GroupAndDirectHaveIndependentRoutes()
        {
            var route = ChatRepository.GroupKey("agent-room");
            using (var db = Open())
            {
                db.EnsureConversation("aster", "acct_owner");
                var directId = db.Conversation("aster").OptString("id");
                Assert.That(db.MergeConversation(GroupConversation(), "acct_owner"), Is.EqualTo(route));
                Assert.That(ChatRepository.IsGroupKey(route), Is.True);
                Assert.That(ChatRepository.IsGroupKey("aster"), Is.False);
                Assert.That(db.Conversation("aster").OptString("id"), Is.EqualTo(directId));
                Assert.That(db.Conversation(route).OptString("id"), Is.EqualTo("agent-room"));
                Assert.That(db.ConversationById("agent-room").OptString("kind"), Is.EqualTo("group"));
            }
        }

        [Test]
        public void GroupOutboxHistoryAndCursorSurviveReopen()
        {
            var route = ChatRepository.GroupKey("agent-room");
            using (var db = Open())
            {
                db.MergeConversation(GroupConversation(), "acct_owner");
                db.MergeMessages(new JArray().Put(GroupMessage("msg_group_aster", 1, "アスターです", "aster")));
                db.Enqueue(route, "acct_owner", "主人", Parts("みんなへ"), "cm_group", 1002);
                db.SetCursor("agent-room", "1");
                Assert.That(db.Pending().Single().ConversationId, Is.EqualTo("agent-room"));
                Assert.That(db.Messages(route).Select(row => row.Json.OptString("id")),
                    Is.EqualTo(new[] { "msg_group_aster", "local_cm_group" }));
            }
            using (var db = Open())
            {
                Assert.That(db.Cursor("agent-room"), Is.EqualTo("1"));
                Assert.That(db.Pending().Single().ConversationId, Is.EqualTo("agent-room"));
                var messages = db.Messages(route);
                Assert.That(messages.Count, Is.EqualTo(2));
                Assert.That(messages[1].Pending, Is.True);
                Assert.That(messages[1].Json.OptJArray("parts").OptJObject(0).OptString("text"),
                    Is.EqualTo("みんなへ"));
            }
        }

        [Test]
        public void GroupMessagesKeepRealAuthorsOutOfDirectHistory()
        {
            var route = ChatRepository.GroupKey("agent-room");
            using (var db = Open())
            {
                db.EnsureConversation("aster", "acct_owner");
                db.MergeConversation(GroupConversation(), "acct_owner");
                db.MergeMessages(new JArray().Put(GroupMessage("msg_group_aster", 1, "アスターです", "aster"))
                    .Put(GroupMessage("msg_group_nori", 2, "ノリです", "nori")));
                var group = db.Messages(route);
                Assert.That(group.Select(row => row.Json.OptJObject("author").OptString("id")),
                    Is.EqualTo(new[] { "aster", "nori" }));
                Assert.That(group.Select(row => row.Json.OptJArray("parts").OptJObject(0).OptString("text")),
                    Is.EqualTo(new[] { "アスターです", "ノリです" }));
                Assert.That(db.Messages("aster"), Is.Empty);
            }
            using (var db = Open())
            {
                Assert.That(db.Messages(route).Select(row => row.Json.OptJObject("author").OptString("id")),
                    Is.EqualTo(new[] { "aster", "nori" }));
                Assert.That(db.Messages("aster"), Is.Empty);
            }
        }

        [Test]
        public void CanonicalAccountMergesSummaryAndPreservesLegacyPendingOutbox()
        {
            var legacyId = ChatRepository.DirectId("owner", "aster");
            var canonicalId = ChatRepository.DirectId("acct_owner", "aster");
            var latest = new JObject().Put("id", "msg_live").Put("seq", 20)
                .Put("conversation_id", canonicalId).Put("created_at", 1020.0)
                .Put("author", new JObject().Put("id", "aster").Put("kind", "entity"))
                .Put("parts", Parts("サーバー履歴"));
            var incoming = new JObject().Put("id", canonicalId).Put("kind", "direct")
                .Put("created_at", 1000.0).Put("unread", 1).Put("last_message", latest)
                .Put("participants", JArray.OfStrings(new[] { "acct_owner", "aster" }));
            using (var db = Open())
            {
                db.Enqueue("aster", "owner", "主人", Parts("保留待送"), "cm_legacy", 1001);
                Assert.That(db.Conversation("aster").OptString("id"), Is.EqualTo(legacyId));
                Assert.That(db.MergeConversation(incoming, "acct_owner"), Is.EqualTo("aster"));
                Assert.That(db.Conversation("aster").OptString("id"), Is.EqualTo(canonicalId));
                Assert.That(db.ConversationById(legacyId), Is.Null);
                Assert.That(db.Conversation("aster").OptJObject("last_message").OptString("id"), Is.EqualTo("msg_live"));
                Assert.That(db.Conversation("aster").OptInt("unread"), Is.EqualTo(1));
                Assert.That(db.Pending().Single().ConversationId, Is.EqualTo(canonicalId));
                Assert.That(db.Messages("aster").Single().Json.OptString("conversation_id"), Is.EqualTo(canonicalId));
            }
            using (var db = Open())
            {
                Assert.That(db.Pending().Single().Id, Is.EqualTo("cm_legacy"));
                Assert.That(db.Messages("aster").Single().Json.OptJArray("parts").OptJObject(0).OptString("text"),
                    Is.EqualTo("保留待送"));
            }
        }
    }
}
