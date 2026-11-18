using System;
using System.Collections.Generic;
using System.Linq;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>
    /// `SseFramePump` 的離線判據（2026-11-04 P0-a 止血）。
    ///
    /// 這幾條守的是**真實踩過的資料遺失**：某幀的處理器一拋例外，舊寫法會把同一批裡後面的幀
    /// 一起丟掉、而且游標已經推過去（重連也補不回來）。⇒ 這裡把「失敗不前進、其餘照走」釘死。
    /// </summary>
    [TestFixture]
    public class SseFramePumpTests
    {
        private static SseFrame F(string id, string ev = "delivery", string data = "{}")
        {
            return new SseFrame { Id = id, Event = ev, Data = data };
        }

        [Test]
        public void 處理成功才推進游標()
        {
            var seen = new List<string>();
            var cursors = new List<string>();
            var n = SseFramePump.Pump(
                new List<SseFrame> { F("1"), F("2"), F("3") },
                f => { seen.Add(f.Id); return true; },
                id => cursors.Add(id));

            Assert.That(n, Is.EqualTo(3));
            Assert.That(seen, Is.EqualTo(new[] { "1", "2", "3" }));
            Assert.That(cursors, Is.EqualTo(new[] { "1", "2", "3" }));
        }

        [Test]
        public void 某幀拋例外_後面的幀照樣處理但游標不越過缺口()
        {
            var seen = new List<string>();
            var cursors = new List<string>();
            var errors = new List<string>();

            var n = SseFramePump.Pump(
                new List<SseFrame> { F("1"), F("2"), F("3") },
                f =>
                {
                    seen.Add(f.Id);
                    if (f.Id == "2") throw new InvalidOperationException("boom");
                    return true;
                },
                id => cursors.Add(id),
                (f, e) => errors.Add(f.Id + ":" + e.GetType().Name));

            // 3 仍要處理，但不能把游標推過 2；否則重連只補 3 之後，2 會永久丟失。
            Assert.That(seen, Is.EqualTo(new[] { "1", "2", "3" }), "後面的幀必須照樣處理");
            Assert.That(cursors, Is.EqualTo(new[] { "1" }), "游標必須停在最後一個連續成功幀");
            Assert.That(errors, Is.EqualTo(new[] { "2:InvalidOperationException" }));
            Assert.That(n, Is.EqualTo(2));
        }

        [Test]
        public void 失敗缺口跨後續批次仍堵住游標()
        {
            var cursors = new List<string>();
            var cursorBlocked = false;
            SseFramePump.Pump(
                new List<SseFrame> { F("1"), F("2") },
                f => f.Id != "2" ? true : throw new InvalidOperationException("boom"),
                id => cursors.Add(id), ref cursorBlocked);
            SseFramePump.Pump(new List<SseFrame> { F("3") }, f => true,
                              id => cursors.Add(id), ref cursorBlocked);

            Assert.That(cursorBlocked, Is.True);
            Assert.That(cursors, Is.EqualTo(new[] { "1" }),
                        "下一批的第 3 幀也不能越過上一批失敗的第 2 幀");
        }

        [Test]
        public void 處理器回false_不推進游標()
        {
            var cursors = new List<string>();
            // `ready` 那種控制幀：算處理完、但不該吃掉游標
            var n = SseFramePump.Pump(
                new List<SseFrame> { F("0", "ready"), F("1") },
                f => f.Event != "ready",
                id => cursors.Add(id));

            Assert.That(cursors, Is.EqualTo(new[] { "1" }));
            Assert.That(n, Is.EqualTo(1));
        }

        [Test]
        public void 錯誤回呼自己炸掉也不能吃掉後面的幀()
        {
            var cursors = new List<string>();
            var n = SseFramePump.Pump(
                new List<SseFrame> { F("1"), F("2") },
                f => { if (f.Id == "1") throw new Exception("boom"); return true; },
                id => cursors.Add(id),
                (f, e) => throw new Exception("error handler boom"));

            Assert.That(cursors, Is.Empty, "第 2 幀雖照常處理，也不能越過第 1 幀的失敗缺口");
            Assert.That(n, Is.EqualTo(1));
        }

        [Test]
        public void 空清單與null都不炸()
        {
            Assert.That(SseFramePump.Pump(null, f => true, id => { }), Is.EqualTo(0));
            Assert.That(SseFramePump.Pump(new List<SseFrame>(), f => true, id => { }), Is.EqualTo(0));
        }

        [Test]
        public void 沒有給處理器與游標也不炸()
        {
            Assert.That(SseFramePump.Pump(new List<SseFrame> { F("1") }, null, null), Is.EqualTo(1));
        }

        [Test]
        public void 順序照伺服器送來的順序()
        {
            var cursors = new List<string>();
            SseFramePump.Pump(
                new List<SseFrame> { F("a"), F("b"), F("c"), F("d") },
                f => true,
                id => cursors.Add(id));
            Assert.That(cursors, Is.EqualTo(new[] { "a", "b", "c", "d" }));
        }
    }
}
