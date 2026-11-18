using System.Linq;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    [TestFixture]
    public class ConversationStoreTests
    {
        [Test]
        public void 空內容與空角色不進未讀()
        {
            var store = new ConversationStore();
            Assert.That(store.Accept("", "t1", "hello"), Is.EqualTo(ConversationStore.Route.Ignore));
            Assert.That(store.Accept("aster", "t2", ""), Is.EqualTo(ConversationStore.Route.Ignore));
            Assert.That(store.TotalUnread(), Is.EqualTo(0));
        }

        [Test]
        public void 同一turn只收一次且按角色分開()
        {
            var store = new ConversationStore();
            Assert.That(store.Accept("aster", "t1", "一"), Is.EqualTo(ConversationStore.Route.Added));
            Assert.That(store.Accept("aster", "t1", "一"), Is.EqualTo(ConversationStore.Route.Duplicate));
            Assert.That(store.Accept("nori", "t2", "二"), Is.EqualTo(ConversationStore.Route.Added));
            Assert.That(store.Unread("aster"), Is.EqualTo(1));
            Assert.That(store.Unread("nori"), Is.EqualTo(1));
            Assert.That(store.TotalUnread(), Is.EqualTo(2));
        }

        [Test]
        public void Take依序交出並清除已讀()
        {
            var store = new ConversationStore();
            store.Accept("aster", "t1", "一", 10, "push", "a1");
            store.Accept("aster", "t2", "二", 11);
            var rows = store.Take("aster");
            Assert.That(rows.Select(row => row.Text), Is.EqualTo(new[] { "一", "二" }));
            Assert.That(rows[0].At, Is.EqualTo(10));
            Assert.That(rows[0].Audio, Is.EqualTo("a1"));
            Assert.That(store.Unread("aster"), Is.EqualTo(0));
            Assert.That(store.Take("aster"), Is.Empty);
        }

        [Test]
        public void Preview保留未讀且外部改副本不污染暫存()
        {
            var store = new ConversationStore();
            store.Accept("aster", "t1", "原文", 10, "push", "a1");
            var preview = store.Preview("aster");
            preview[0].Text = "畫面修改";
            preview[0].Audio = "";
            Assert.That(store.Unread("aster"), Is.EqualTo(1), "渲染前的 Preview 不能清未讀");
            var taken = store.Take("aster").Single();
            Assert.That(taken.Text, Is.EqualTo("原文"));
            Assert.That(taken.Audio, Is.EqualTo("a1"));
            Assert.That(store.Unread("aster"), Is.EqualTo(0));
        }

        [Test]
        public void 快照重啟後保留未讀順序與去重表()
        {
            var original = new ConversationStore();
            original.Accept("aster", "t1", "一");
            original.Accept("nori", "t2", "二");
            original.Accept("mira", "t3", "三");

            var restored = new ConversationStore(original.Snapshot());
            Assert.That(restored.TotalUnread(), Is.EqualTo(3));
            Assert.That(restored.Accept("aster", "t1", "重播"), Is.EqualTo(ConversationStore.Route.Duplicate));
            Assert.That(restored.Take("aster").Single().Text, Is.EqualTo("一"));
            Assert.That(restored.TotalUnread(), Is.EqualTo(2));
        }

        [Test]
        public void 未讀窗口有界且保留最新內容()
        {
            var store = new ConversationStore();
            for (var i = 0; i < ConversationStore.PendingMaxPerEntity + 2; i++)
                store.Accept("aster", "t" + i, "msg" + i);
            var rows = store.Take("aster");
            Assert.That(rows.Count, Is.EqualTo(ConversationStore.PendingMaxPerEntity));
            Assert.That(rows.First().Text, Is.EqualTo("msg2"));
            Assert.That(rows.Last().Text, Is.EqualTo("msg" + (ConversationStore.PendingMaxPerEntity + 1)));
        }

        [Test]
        public void 去重窗口有界()
        {
            var store = new ConversationStore();
            for (var i = 0; i < ConversationStore.SeenMax + 1; i++)
                store.Accept("aster", "t" + i, "msg" + i);
            Assert.That(store.Accept("aster", "t1", "仍在窗口內"),
                        Is.EqualTo(ConversationStore.Route.Duplicate));
            Assert.That(store.Accept("aster", "t0", "最舊的可再收"),
                        Is.EqualTo(ConversationStore.Route.Added));
        }
    }
}
