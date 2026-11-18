using System;
using System.IO;
using System.Linq;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>S2 真 SQLite：不是記憶體 fake；每個案例都實際關閉再開啟資料庫。</summary>
    public sealed class SocialRepositoryTest
    {
        private string _root;
        private string _path;

        [SetUp]
        public void SetUp()
        {
            _root = Path.Combine(Path.GetTempPath(), "ba-social-" + Guid.NewGuid().ToString("N"));
            _path = Path.Combine(_root, "owner", "social.db");
        }

        [TearDown]
        public void TearDown()
        {
            if (Directory.Exists(_root)) Directory.Delete(_root, true);
        }

        private static SocialRepository.Author Owner()
        {
            return new SocialRepository.Author("owner", "owner", "主人");
        }

        private SocialRepository Open() { return new SocialRepository(_path); }

        [Test]
        public void OfflinePostAndOutboxSurviveReopen()
        {
            using (var db = Open())
            {
                var item = db.EnqueuePost("オフライン投稿", JArray.OfStrings(new[] { "m1" }),
                    Owner(), 1000, "cm_post");
                Assert.That(item.Pending, Is.True);
                Assert.That(db.Feed().Single().Json.OptString("body"), Is.EqualTo("オフライン投稿"));
            }
            using (var db = Open())
            {
                var post = db.Feed().Single();
                Assert.That(post.Json.OptJArray("media").OptString(0), Is.EqualTo("m1"));
                Assert.That(post.Pending, Is.True);
                Assert.That(db.PendingMutations().Single().Id, Is.EqualTo("cm_post"));
                Assert.That(db.Mutation("cm_post").Payload.OptString("text"), Is.EqualTo("オフライン投稿"));
            }
        }

        [Test]
        public void SameClientMutationDoesNotInsertTwice()
        {
            using (var db = Open())
            {
                var first = db.EnqueuePost("一次", new JArray(), Owner(), 1000, "cm_same");
                var second = db.EnqueuePost("改過也不能變第二筆", new JArray(), Owner(), 1001, "cm_same");
                Assert.That(second.Json.OptString("id"), Is.EqualTo(first.Json.OptString("id")));
                Assert.That(db.Feed().Count, Is.EqualTo(1));
                Assert.That(db.AllMutations().Count, Is.EqualTo(1));
            }
        }

        [Test]
        public void AcceptedResponseReplacesLocalIdAndKeepsEvidence()
        {
            using (var db = Open())
            {
                db.EnqueuePost("待確認", new JArray(), Owner(), 1000, "cm_accept");
                var server = new JObject().Put("id", "pst_server").Put("at", 1002.0)
                    .Put("author_id", "owner").Put("author_kind", "owner").Put("body", "已確認")
                    .Put("media", new JArray()).Put("likes", 0).Put("replies", 0).Put("liked", false);
                db.MarkAccepted("cm_accept", new JObject().Put("post", server), 1003);
                Assert.That(db.Item("local_cm_accept"), Is.Null);
                Assert.That(db.Item("pst_server").Pending, Is.False);
                Assert.That(db.Feed().Single().Json.OptString("body"), Is.EqualTo("已確認"));
                Assert.That(db.Mutation("cm_accept").State, Is.EqualTo("accepted"));
                Assert.That(db.Mutation("cm_accept").Result.OptJObject("post").OptString("id"),
                    Is.EqualTo("pst_server"));
                Assert.That(db.PendingMutations(), Is.Empty);
            }
            using (var db = Open()) { Assert.That(db.Item("pst_server"), Is.Not.Null); }
        }

        [Test]
        public void TransportFailureRetriesButBusinessFailureNeedsExplicitRetry()
        {
            using (var db = Open())
            {
                db.EnqueuePost("網路失敗", new JArray(), Owner(), 1000, "cm_retry");
                db.MarkAttempt("cm_retry", 1001);
                db.MarkFailed("cm_retry", "offline", true, 1002);
                Assert.That(db.Mutation("cm_retry").Attempts, Is.EqualTo(1));
                Assert.That(db.PendingMutations().Single().Error, Is.EqualTo("offline"));
                db.MarkFailed("cm_retry", "rate_limited", false, 1003);
                Assert.That(db.PendingMutations(), Is.Empty);
                Assert.That(db.Mutation("cm_retry").State, Is.EqualTo("failed"));
                db.RetryFailed(1004);
                Assert.That(db.PendingMutations().Single().Error, Is.EqualTo(""));
            }
        }

        [Test]
        public void ReplyLikeAndDeleteUseSameLocalProjection()
        {
            using (var db = Open())
            {
                db.MergeFeed(new JArray().Put(new JObject().Put("id", "pst_1").Put("at", 1000.0)
                    .Put("body", "原帖").Put("media", new JArray()).Put("likes", 2)
                    .Put("replies", 0).Put("liked", false)));
                db.EnqueueReply("pst_1", "離線回覆", new JArray(), Owner(), 1001, "cm_reply");
                db.EnqueueLike("pst_1", Owner(), 1002, "cm_like");
                Assert.That(db.Replies("pst_1").Single().Pending, Is.True);
                Assert.That(db.Item("pst_1").Json.OptInt("likes"), Is.EqualTo(3));
                Assert.That(db.Item("pst_1").Json.OptBoolean("liked"), Is.True);
                db.EnqueueDelete("pst_1", false, 1003, "cm_delete");
                Assert.That(db.Feed(), Is.Empty);
                Assert.That(db.PendingMutations().Select(row => row.Kind),
                    Is.EqualTo(new[] { "reply.create", "post.like", "post.delete" }));
            }
        }

        [Test]
        public void RejectedDeleteRestoresVisiblePost()
        {
            using (var db = Open())
            {
                db.MergeFeed(new JArray().Put(new JObject().Put("id", "pst_1").Put("at", 1000.0)
                    .Put("body", "原帖").Put("media", new JArray())));
                db.EnqueueDelete("pst_1", false, 1001, "cm_denied");
                db.MarkFailed("cm_denied", "delete_denied", false, 1002);
                Assert.That(db.Feed().Single().Pending, Is.False);
                Assert.That(db.Mutation("cm_denied").Error, Is.EqualTo("delete_denied"));
            }
        }

        [Test]
        public void AcceptingParentRemapsDependentOfflineMutations()
        {
            using (var db = Open())
            {
                db.EnqueuePost("根帖", new JArray(), Owner(), 1000, "cm_root");
                db.EnqueueReply("local_cm_root", "回覆", new JArray(), Owner(), 1001, "cm_child");
                db.EnqueueLike("local_cm_root", Owner(), 1002, "cm_like");
                db.EnqueueDelete("local_cm_root", false, 1003, "cm_delete");
                var server = new JObject().Put("id", "pst_server").Put("at", 1004.0)
                    .Put("body", "根帖").Put("media", new JArray()).Put("likes", 0)
                    .Put("replies", 0).Put("liked", false);
                db.MarkAccepted("cm_root", new JObject().Put("post", server), 1005);
                Assert.That(db.Mutation("cm_child").Payload.OptString("id"), Is.EqualTo("pst_server"));
                Assert.That(db.Mutation("cm_like").Payload.OptString("id"), Is.EqualTo("pst_server"));
                Assert.That(db.Mutation("cm_delete").Payload.OptString("id"), Is.EqualTo("pst_server"));
                Assert.That(db.Replies("pst_server").Single().Json.OptString("parent_id"),
                    Is.EqualTo("pst_server"));
            }
        }

        [Test]
        public void AcceptedLikeUsesServerCountAndClearsPending()
        {
            using (var db = Open())
            {
                db.MergeFeed(new JArray().Put(new JObject().Put("id", "pst_like").Put("at", 1000.0)
                    .Put("body", "按讚").Put("media", new JArray()).Put("likes", 2).Put("liked", false)));
                db.EnqueueLike("pst_like", Owner(), 1001, "cm_like_ok");
                var server = new JObject().Put("id", "pst_like").Put("at", 1000.0)
                    .Put("body", "按讚").Put("media", new JArray()).Put("likes", 8).Put("liked", true);
                db.MarkAccepted("cm_like_ok", new JObject().Put("post", server), 1002);
                var item = db.Item("pst_like");
                Assert.That(item.Pending, Is.False);
                Assert.That(item.Json.OptInt("likes"), Is.EqualTo(8));
                Assert.That(item.Json.OptBoolean("liked"), Is.True);
            }
        }

        [Test]
        public void RejectedLikeRestoresProjectionWithoutClearingDelete()
        {
            using (var db = Open())
            {
                db.MergeFeed(new JArray().Put(new JObject().Put("id", "pst_like_fail").Put("at", 1000.0)
                    .Put("body", "原狀態").Put("media", new JArray()).Put("likes", 4).Put("liked", false)));
                db.EnqueueLike("pst_like_fail", Owner(), 1001, "cm_like_fail");
                db.EnqueueDelete("pst_like_fail", false, 1002, "cm_delete_wait");
                db.MarkFailed("cm_like_fail", "forbidden", false, 1003);
                var item = db.Item("pst_like_fail");
                Assert.That(item.Json.OptInt("likes"), Is.EqualTo(4));
                Assert.That(item.Json.OptBoolean("liked"), Is.False);
                Assert.That(item.Pending, Is.True);
                Assert.That(item.MutationId, Is.EqualTo("cm_delete_wait"));
                Assert.That(db.Feed(), Is.Empty);
            }
        }

        [Test]
        public void AcceptedMutationReplayDoesNotCreateAnotherPostAfterReopen()
        {
            using (var db = Open())
            {
                db.EnqueuePost("一次", new JArray(), Owner(), 1000, "cm_replay");
                var server = new JObject().Put("id", "pst_replay").Put("at", 1001.0)
                    .Put("body", "一次").Put("media", new JArray());
                db.MarkAccepted("cm_replay", new JObject().Put("post", server), 1002);
            }
            using (var db = Open())
            {
                var again = db.EnqueuePost("重送", new JArray(), Owner(), 1003, "cm_replay");
                Assert.That(again.Json.OptString("id"), Is.EqualTo("pst_replay"));
                Assert.That(again.Pending, Is.False);
                Assert.That(db.Feed().Count, Is.EqualTo(1));
                Assert.That(db.AllMutations().Count, Is.EqualTo(1));
            }
        }

        [Test]
        public void WholeOfflineChainRemapsIdsAndSettlesAfterReopen()
        {
            using (var db = Open())
            {
                db.EnqueuePost("根帖", new JArray(), Owner(), 1000, "cm_chain_root");
                db.EnqueueReply("local_cm_chain_root", "回覆", new JArray(), Owner(), 1001, "cm_chain_reply");
                db.EnqueueLike("local_cm_chain_root", Owner(), 1002, "cm_chain_like");
                db.EnqueueDelete("local_cm_chain_reply", true, 1003, "cm_chain_reply_delete");
                db.EnqueueDelete("local_cm_chain_root", false, 1004, "cm_chain_root_delete");

                var root = new JObject().Put("id", "pst_chain_root").Put("at", 1005.0)
                    .Put("body", "根帖").Put("media", new JArray()).Put("likes", 0).Put("liked", false);
                db.MarkAccepted("cm_chain_root", new JObject().Put("post", root), 1006);
                Assert.That(db.Mutation("cm_chain_reply").Payload.OptString("id"), Is.EqualTo("pst_chain_root"));
                Assert.That(db.Mutation("cm_chain_like").Payload.OptString("id"), Is.EqualTo("pst_chain_root"));
                Assert.That(db.Mutation("cm_chain_root_delete").Payload.OptString("id"), Is.EqualTo("pst_chain_root"));

                var reply = new JObject().Put("id", "pst_chain_reply").Put("parent_id", "pst_chain_root")
                    .Put("at", 1007.0).Put("body", "回覆").Put("media", new JArray());
                db.MarkAccepted("cm_chain_reply", new JObject().Put("post", reply), 1008);
                Assert.That(db.Mutation("cm_chain_reply_delete").Payload.OptString("id"),
                    Is.EqualTo("pst_chain_reply"));
                root.Put("likes", 1).Put("liked", true);
                db.MarkAccepted("cm_chain_like", new JObject().Put("post", root), 1009);
                db.MarkAccepted("cm_chain_reply_delete", new JObject().Put("ok", true), 1010);
                db.MarkAccepted("cm_chain_root_delete", new JObject().Put("ok", true), 1011);
            }
            using (var db = Open())
            {
                Assert.That(db.PendingMutations(), Is.Empty);
                Assert.That(db.Feed(), Is.Empty);
                Assert.That(db.Replies("pst_chain_root"), Is.Empty);
                Assert.That(db.AllMutations().Select(row => row.State),
                    Is.All.EqualTo("accepted"));
                Assert.That(db.Item("pst_chain_root").Pending, Is.False);
                Assert.That(db.Item("pst_chain_reply", true).Pending, Is.False);
            }
        }

        [Test]
        public void EventsAreReplaySafeAndCursorPersists()
        {
            using (var db = Open())
            {
                var post = new JObject().Put("id", "pst_event").Put("at", 1000.0)
                    .Put("body", "事件").Put("media", new JArray());
                var created = new JObject().Put("event_id", "evt_1").Put("type", "post.created")
                    .Put("payload", new JObject().Put("post", post));
                db.ApplyEvent(created);
                db.ApplyEvent(created);
                Assert.That(db.Feed().Count, Is.EqualTo(1));
                db.SetCursor("feed", "42");
                var deleted = new JObject().Put("event_id", "evt_2").Put("type", "post.deleted")
                    .Put("payload", new JObject().Put("post_id", "pst_event"));
                db.ApplyEvent(deleted);
                Assert.That(db.Feed(), Is.Empty);
            }
            using (var db = Open()) { Assert.That(db.Cursor("feed"), Is.EqualTo("42")); }
        }

        [Test]
        public void FailedEnqueueRollsBackBothProjectionAndOutbox()
        {
            using (var db = Open())
            {
                Assert.Throws<InvalidOperationException>(() =>
                    db.EnqueueLike("pst_missing", Owner(), 1000, "cm_missing"));
                Assert.That(db.AllMutations(), Is.Empty);
                Assert.That(db.Feed(), Is.Empty);
            }
        }

        [Test]
        public void PublicMoodPersistsOnlyPublicFieldsAndSurvivesReopen()
        {
            using (var db = Open())
            {
                db.MergeMood(new JObject().Put("account_id", "aster").Put("kind", "entity")
                    .Put("display", "アスター").Put("key", "happy").Put("note", "公開")
                    .Put("updated_at", 1000.0).Put("event_id", "evt_mood")
                    .Put("valence", 0.9).Put("arousal", 1.0).Put("social_need", 1.0)
                    .Put("affect", new JObject().Put("private", true)));
                var raw = db.Mood("aster").ToString();
                Assert.That(raw, Does.Not.Contain("valence"));
                Assert.That(raw, Does.Not.Contain("social_need"));
                Assert.That(raw, Does.Not.Contain("private"));
            }
            using (var db = Open())
            {
                Assert.That(db.Mood("aster").OptString("key"), Is.EqualTo("happy"));
                Assert.That(db.Mood("aster").OptString("note"), Is.EqualTo("公開"));
            }
        }

        [Test]
        public void MoodSnapshotReplacesVisibleDomainAndStaleWritesDoNotRegress()
        {
            using (var db = Open())
            {
                db.MergeMood(new JObject().Put("account_id", "nori").Put("key", "calm").Put("updated_at", 1000.0));
                db.MergeMood(new JObject().Put("account_id", "aster").Put("key", "happy").Put("updated_at", 1000.0));
                db.ReplaceMoods(new JArray().Put(new JObject().Put("account_id", "aster")
                    .Put("key", "excited").Put("note", "新鮮").Put("updated_at", 1002.0)));
                Assert.That(db.Mood("nori"), Is.Null);
                db.MergeMood(new JObject().Put("account_id", "aster").Put("key", "down")
                    .Put("note", "舊回包").Put("updated_at", 1001.0));
                Assert.That(db.Mood("aster").OptString("key"), Is.EqualTo("excited"));
                Assert.That(db.Mood("aster").OptString("note"), Is.EqualTo("新鮮"));
            }
        }

        [Test]
        public void MoodEventsAreReplaySafeAndMonotonic()
        {
            using (var db = Open())
            {
                var mood = new JObject().Put("account_id", "aster").Put("key", "lonely")
                    .Put("note", "公開短句").Put("updated_at", 1000.0);
                var envelope = new JObject().Put("event_id", "evt_mood_1").Put("type", "mood.updated")
                    .Put("payload", new JObject().Put("mood", mood));
                db.ApplyEvent(envelope);
                db.ApplyEvent(envelope);
                Assert.That(db.Moods().Count, Is.EqualTo(1));
                Assert.That(db.Mood("aster").OptString("key"), Is.EqualTo("lonely"));
            }
        }
    }
}
