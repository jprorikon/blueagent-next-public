using System.Collections.Generic;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>
    /// Next 端末的會話狀態（unity-review 2026-10-28 U-1～U-6）：這幾支是 `NextApi`／`NextShell`／
    /// `NextChatView` 背後的規則，抽到共用層才釘得住觸發順序。
    /// </summary>
    public class NextSessionLogicTest
    {
        private sealed class MemPrefs : IPrefs
        {
            public readonly Dictionary<string, string> Rows = new Dictionary<string, string>();
            public string Get(string key) => Rows.TryGetValue(key, out var value) ? value : "";
            public void Set(string key, string value) => Rows[key] = value;
            public void Delete(string key) => Rows.Remove(key);
        }

        private const string Server = "http://nas:8787";

        // ── U-1：事件游標按「伺服器＋身份」分區 ─────────────────────────────────

        [Test]
        public void U1_換帳號不帶別人的游標()
        {
            var prefs = new MemPrefs();
            var cursors = new EventCursors(prefs);
            cursors.Bind(Server, "guest-x");
            cursors.Advance("1");                 // guest 收到過 1
            cursors.Unbind();
            cursors.Bind(Server, "owner");
            cursors.Advance("2");
            cursors.Advance("5");                 // owner 一路收到 5
            cursors.Unbind();

            cursors.Bind(Server, "guest-x");
            Assert.That(cursors.Current, Is.EqualTo("1"), "guest 要帶自己的 1 去補 2..，不是 owner 的 5");
        }

        [Test]
        public void U1_往返切換不串不漏()
        {
            var cursors = new EventCursors(new MemPrefs());
            cursors.Bind(Server, "owner"); cursors.Advance("10"); cursors.Unbind();
            cursors.Bind(Server, "guest-x"); cursors.Advance("11"); cursors.Unbind();
            cursors.Bind(Server, "owner");
            Assert.That(cursors.Current, Is.EqualTo("10"));
            cursors.Advance("12"); cursors.Unbind();
            cursors.Bind(Server, "guest-x");
            Assert.That(cursors.Current, Is.EqualTo("11"));
        }

        [Test]
        public void U1_換伺服器不帶舊伺服器的游標()
        {
            var cursors = new EventCursors(new MemPrefs());
            cursors.Bind(Server, "owner"); cursors.Advance("99"); cursors.Unbind();
            cursors.Bind("http://other:8787", "owner");
            Assert.That(cursors.Current, Is.EqualTo(""));
            cursors.Unbind();
            cursors.Bind("HTTP://NAS:8787/", "owner");
            Assert.That(cursors.Current, Is.EqualTo("99"), "大小寫／結尾斜線不同仍是同一台");
        }

        [Test]
        public void U1_重開程式各帳號各自接著補()
        {
            var prefs = new MemPrefs();
            var before = new EventCursors(prefs);
            before.Bind(Server, "owner"); before.Advance("7"); before.Unbind();
            before.Bind(Server, "guest-x"); before.Advance("4");

            var after = new EventCursors(prefs);          // 殺進程重開
            after.Bind(Server, "owner");
            Assert.That(after.Current, Is.EqualTo("7"));
            after.Unbind();
            after.Bind(Server, "guest-x");
            Assert.That(after.Current, Is.EqualTo("4"));
        }

        [Test]
        public void U1_沒綁定不記_舊的全域鍵綁定時丟掉()
        {
            var prefs = new MemPrefs();
            prefs.Set(EventCursors.LegacyKey, "3");
            var cursors = new EventCursors(prefs);
            cursors.Advance("8");
            Assert.That(cursors.Current, Is.EqualTo(""), "登入前／登出後收到的不屬於任何人");
            cursors.Bind(Server, "guest-x");
            Assert.That(cursors.Current, Is.EqualTo(""), "不知道是誰的舊游標不能拿來濾 guest");
            Assert.That(prefs.Rows.ContainsKey(EventCursors.LegacyKey), Is.False);
            Assert.That(EventCursors.KeyFor(Server, ""), Is.EqualTo(""));
        }

        // ── U-2：回合票 ──────────────────────────────────────────────────────────

        [Test]
        public void U2_舊角色的回呼認不出現在這一輪()
        {
            var tickets = new TurnTickets();
            var a = tickets.Begin("mira");
            tickets.Invalidate();                       // 換到 aster
            var b = tickets.Begin("aster");
            Assert.That(tickets.IsCurrent(a), Is.False, "A 的 frame／closed 不准動 B");
            Assert.That(tickets.IsCurrent(b), Is.True);
            Assert.That(b.Entity, Is.EqualTo("aster"));

            var c = tickets.Begin("nori");               // A→B→C 連續切
            Assert.That(tickets.IsCurrent(b), Is.False);
            Assert.That(tickets.IsCurrent(c), Is.True);
            tickets.Invalidate();                       // 登出
            Assert.That(tickets.IsCurrent(c), Is.False);
        }

        [Test]
        public void U2_舊協程收尾不清掉新一輪的句柄()
        {
            var slot = new OwnedSlot<object>();
            var requestA = new object();
            var requestB = new object();
            slot.Set(requestA);
            slot.Set(requestB);                         // B 開始（A 被 Abort，收尾晚一幀）
            Assert.That(slot.ClearIf(requestA), Is.False);
            Assert.That(slot.Value, Is.SameAs(requestB), "B 還要能被取消");
            Assert.That(slot.ClearIf(requestB), Is.True);
            Assert.That(slot.Value, Is.Null);
        }

        // ── U-3／U-6：登入期代次與 401 ────────────────────────────────────────────

        [Test]
        public void U3_登出後舊回呼作廢()
        {
            var gate = new SessionGate();
            var a = gate.Open();
            gate.Close();
            Assert.That(gate.IsLive(a), Is.False, "登出後位置推送的回呼不准動");
            var b = gate.Open();
            Assert.That(gate.IsCurrent(a), Is.False, "帳號 A 的在途請求不准寫進 B");
            Assert.That(gate.IsLive(b), Is.True);
        }

        [Test]
        public void U6_同一段登入的多個401只收口一次()
        {
            var gate = new SessionGate();
            var epoch = gate.Open();
            Assert.That(gate.ReportStatus(401, epoch), Is.True);
            Assert.That(gate.ReportStatus(401, epoch), Is.False, "第二個 401 不再建一次登入頁");
            Assert.That(gate.ReportStatus(401, epoch), Is.False);
        }

        [Test]
        public void U6_403不清token_非401不算()
        {
            var gate = new SessionGate();
            var epoch = gate.Open();
            Assert.That(gate.ReportStatus(403, epoch), Is.False);
            Assert.That(gate.ReportStatus(500, epoch), Is.False);
            Assert.That(gate.ReportStatus(0, epoch), Is.False);
            Assert.That(gate.ReportStatus(401, epoch), Is.True, "403 之後的 401 仍要收口");
        }

        [Test]
        public void U6_上一段或登入前的401不觸發收口()
        {
            var gate = new SessionGate();
            var beforeLogin = gate.Epoch;
            Assert.That(gate.ReportStatus(401, beforeLogin), Is.False, "驗 token 的 401 由登入頁自己講");
            Assert.That(gate.IsCurrent(beforeLogin), Is.True, "登入頁自己的請求要回得來");
            var first = gate.Open();
            gate.Close();
            var second = gate.Open();
            Assert.That(gate.ReportStatus(401, first), Is.False);
            Assert.That(gate.ReportStatus(401, second), Is.True, "新的一段重新計");
        }

        // ── U-4：別的角色的推播先收著 ────────────────────────────────────────────

        [Test]
        public void U4_看著A時B的話收進B的未讀_不進A()
        {
            var inbox = new DeliveryInbox();
            Assert.That(inbox.Accept("mira", "aster", "t1", "ねえ"), Is.EqualTo(DeliveryInbox.Route.Stash));
            Assert.That(inbox.Unread("aster"), Is.EqualTo(1));
            Assert.That(inbox.Unread("mira"), Is.EqualTo(0));
            Assert.That(inbox.Accept("mira", "mira", "t2", "はい"), Is.EqualTo(DeliveryInbox.Route.Show));
            Assert.That(inbox.Accept("mira", "", "", "誰の？"), Is.EqualTo(DeliveryInbox.Route.Show));
        }

        [Test]
        public void U4_切到B只交一次_依原順序()
        {
            var inbox = new DeliveryInbox();
            inbox.Accept("mira", "aster", "t1", "一つ目");
            inbox.Accept("mira", "nori", "t2", "nori の");
            inbox.Accept("mira", "aster", "t3", "二つ目");
            var aster = inbox.Take("aster");
            Assert.That(aster.ConvertAll(item => item.Text), Is.EqualTo(new[] { "一つ目", "二つ目" }));
            Assert.That(inbox.Take("aster"), Is.Empty, "只顯示一次");
            Assert.That(inbox.Unread("aster"), Is.EqualTo(0));
            Assert.That(inbox.Take("nori")[0].TurnId, Is.EqualTo("t2"), "不串角色");
        }

        [Test]
        public void U4_同一個turn只算一次_沒文字的不歸這裡()
        {
            var inbox = new DeliveryInbox();
            inbox.Accept("mira", "aster", "t1", "ねえ");
            Assert.That(inbox.Accept("mira", "aster", "t1", "ねえ"), Is.EqualTo(DeliveryInbox.Route.Duplicate),
                        "重連重播同一條");
            Assert.That(inbox.Unread("aster"), Is.EqualTo(1));
            Assert.That(inbox.Accept("mira", "aster", "t9", ""), Is.EqualTo(DeliveryInbox.Route.Ignore));
        }

        // ── U-5：一次性明文 ──────────────────────────────────────────────────────

        [Test]
        public void U5_換頁回來或登出後不再顯示()
        {
            var secret = new OneShotSecret();
            secret.BeginVisit();
            secret.Put("tok-issued", "guest-1");
            Assert.That(secret.Value, Is.EqualTo("tok-issued"), "簽發後同一次進頁要看得到");
            secret.BeginVisit();                        // 離開帳號頁再進來
            Assert.That(secret.HasValue, Is.False);

            secret.Put("tok-rotated", "guest-1（輪替）");
            secret.Forget();                            // 登出
            Assert.That(secret.Value, Is.EqualTo(""));
            Assert.That(secret.Label, Is.EqualTo(""));
        }
    }
}
