using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>App 側快取（SyncCache）：存、比雜湊、重開還在、超量丟最舊、換帳號分開。</summary>
    public class SyncCacheTest
    {
        private const string Root = "/cache/a";

        private static byte[] Bytes(int size, byte fill = 1)
        {
            var data = new byte[size];
            for (var i = 0; i < size; i++) data[i] = fill;
            return data;
        }

        [Test]
        public void 存了讀得回來_雜湊去掉引號()
        {
            var cache = new SyncCache(new MemFileSystem(), Root, new FakeClock(100));
            Assert.That(cache.Put("/api/stickers/mira/smile", Bytes(10), "W/\"abc\""), Is.True);
            Assert.That(cache.Get("/api/stickers/mira/smile"), Is.EqualTo(Bytes(10)));
            Assert.That(cache.EtagOf("/api/stickers/mira/smile"), Is.EqualTo("abc"));
            Assert.That(cache.Get("/api/stickers/mira/nope"), Is.Null);
            Assert.That(cache.EtagOf("/api/stickers/mira/nope"), Is.EqualTo(""));
        }

        [Test]
        public void 雜湊對上才算新鮮()
        {
            var cache = new SyncCache(new MemFileSystem(), Root, new FakeClock(100));
            cache.Put("k", Bytes(4), "\"h1\"");
            Assert.That(cache.Fresh("k", "h1"), Is.True);
            Assert.That(cache.Fresh("k", "\"h1\""), Is.True, "線上那份帶引號也認");
            Assert.That(cache.Fresh("k", "h2"), Is.False, "線上換了 ⇒ 要重抓");
            Assert.That(cache.Fresh("k", ""), Is.False, "線上沒給雜湊 ⇒ 不能當新鮮");
            cache.Put("plain", Bytes(4), "");
            Assert.That(cache.Fresh("plain", ""), Is.False);
        }

        [Test]
        public void 重開之後還在()
        {
            var fs = new MemFileSystem();
            new SyncCache(fs, Root, new FakeClock(100)).PutText("turns:mira", "{\"turns\":[]}", "d1");
            var reopened = new SyncCache(fs, Root, new FakeClock(200));
            Assert.That(reopened.GetText("turns:mira"), Is.EqualTo("{\"turns\":[]}"));
            Assert.That(reopened.EtagOf("turns:mira"), Is.EqualTo("d1"));
            Assert.That(reopened.TotalBytes, Is.EqualTo(12));
        }

        [Test]
        public void 檔案被系統清掉_當沒有()
        {
            var fs = new MemFileSystem();
            var cache = new SyncCache(fs, Root, new FakeClock(100));
            cache.Put("k", Bytes(8), "h");
            foreach (var name in fs.List(Root))
                if (name.EndsWith(".bin")) fs.Delete(Paths.Join(Root, name));
            Assert.That(cache.Get("k"), Is.Null);
            Assert.That(cache.EtagOf("k"), Is.EqualTo(""), "不然會帶著舊雜湊去問、拿到 304 卻沒有內容");
            Assert.That(cache.TotalBytes, Is.EqualTo(0));
        }

        [Test]
        public void 超量從最久沒用的開始丟()
        {
            var clock = new FakeClock(100);
            var cache = new SyncCache(new MemFileSystem(), Root, clock, budgetBytes: 30);
            cache.Put("a", Bytes(10), "a");
            clock.Advance(1, 1000);
            cache.Put("b", Bytes(10), "b");
            clock.Advance(1, 1000);
            cache.Put("c", Bytes(10), "c");
            clock.Advance(1, 1000);
            cache.Get("a");                                  // a 剛用過 ⇒ 最舊的是 b
            clock.Advance(1, 1000);
            cache.Put("d", Bytes(10), "d");
            Assert.That(cache.Get("b"), Is.Null);
            Assert.That(cache.Get("a"), Is.Not.Null);
            Assert.That(cache.Get("d"), Is.Not.Null);
            Assert.That(cache.TotalBytes, Is.LessThanOrEqualTo(30));
            Assert.That(cache.Put("huge", Bytes(31), "x"), Is.False, "一份就超過上限：不存");
        }

        [Test]
        public void 同一個網址蓋掉舊的()
        {
            var cache = new SyncCache(new MemFileSystem(), Root, new FakeClock(100));
            cache.Put("k", Bytes(10, 1), "old");
            cache.Put("k", Bytes(4, 2), "new");
            Assert.That(cache.Get("k"), Is.EqualTo(Bytes(4, 2)));
            Assert.That(cache.EtagOf("k"), Is.EqualTo("new"));
            Assert.That(cache.TotalBytes, Is.EqualTo(4));
            cache.Remove("k");
            Assert.That(cache.Count, Is.EqualTo(0));
            Assert.That(cache.TotalBytes, Is.EqualTo(0));
        }

        [Test]
        public void 換帳號換伺服器_目錄不同()
        {
            var owner = SyncCache.FolderFor("https://nas:8443/", "owner");
            Assert.That(owner, Is.EqualTo(SyncCache.FolderFor("https://NAS:8443", "owner")));
            Assert.That(owner, Is.Not.EqualTo(SyncCache.FolderFor("https://nas:8443", "guest-x")));
            Assert.That(owner, Is.Not.EqualTo(SyncCache.FolderFor("https://other:8443", "owner")));
            Assert.That(SyncCache.FolderFor("https://nas:8443", ""), Is.EqualTo(""), "還沒登入：不給目錄");
        }

        /// <summary>
        /// 端末自己算的雜湊必須跟伺服器 `digest.content_tag` 一模一樣（回應沒帶 ETag 時拿它頂上）。
        /// 同一組向量也釘在 `tests/test_sync_cache.py`：兩邊任何一邊改算法，兩邊都會紅。
        /// </summary>
        [Test]
        public void 本機雜湊跟伺服器同一個算法()
        {
            var all = new byte[256];
            for (var i = 0; i < 256; i++) all[i] = (byte)i;
            Assert.That(SyncCache.ContentTag(new byte[0]), Is.EqualTo("e3b0c44298fc1c14"));
            Assert.That(SyncCache.ContentTag(System.Text.Encoding.UTF8.GetBytes("スタンプ")),
                        Is.EqualTo("57a0d8fbec6f9b84"));
            Assert.That(SyncCache.ContentTag(all), Is.EqualTo("40aff2e9d2d8922e"));
            Assert.That(SyncCache.ContentTag(null), Is.EqualTo("e3b0c44298fc1c14"));
        }

        [Test]
        public void 用本機雜湊存的_清單雜湊對得上就新鮮()
        {
            var cache = new SyncCache(new MemFileSystem(), Root, new FakeClock(100));
            var png = Bytes(32, 7);
            cache.Put("/api/stickers/mira/smile", png, SyncCache.ContentTag(png));
            Assert.That(cache.Fresh("/api/stickers/mira/smile", SyncCache.ContentTag(png)), Is.True);
        }

        [Test]
        public void 平台原圖七天過期_縮圖與聊天保留_重開也生效()
        {
            var fs = new MemFileSystem();
            var clock = new FakeClock(100);
            var cache = new SyncCache(fs, Root, clock);
            cache.Put("/v1/media/m0123456789abcdef", Bytes(20, 1), "new");
            cache.Put("/api/media/mfedcba9876543210", Bytes(20, 2), "old");
            cache.Put("/v1/media/m0123456789abcdef?w=360", Bytes(8, 3), "thumb");
            cache.PutText("turns:mira:phone,api", "{\"turns\":[]}", "chat");

            clock.Advance(6 * 24 * 60 * 60, 0);
            var sixDays = new SyncCache(fs, Root, clock);
            Assert.That(sixDays.Get("/v1/media/m0123456789abcdef"), Is.Not.Null, "六天仍可讀");
            Assert.That(sixDays.Get("/api/media/mfedcba9876543210"), Is.Not.Null, "舊 key 過渡期同規則");

            clock.Advance(2 * 24 * 60 * 60, 0);
            var eightDays = new SyncCache(fs, Root, clock);
            Assert.That(eightDays.Get("/v1/media/m0123456789abcdef"), Is.Null, "八天原圖刪除");
            Assert.That(eightDays.Get("/api/media/mfedcba9876543210"), Is.Null);
            Assert.That(eightDays.Get("/v1/media/m0123456789abcdef?w=360"), Is.EqualTo(Bytes(8, 3)),
                        "縮圖不按原圖七天刪");
            Assert.That(eightDays.GetText("turns:mira:phone,api"), Is.EqualTo("{\"turns\":[]}"),
                        "聊天內容與索引不是原圖快取");
            Assert.That(eightDays.Put("/v1/media/m0123456789abcdef", Bytes(20, 9), "redownload"), Is.True);
            Assert.That(eightDays.Get("/v1/media/m0123456789abcdef"), Is.EqualTo(Bytes(20, 9)),
                        "過期後允許重新下載");
        }

        [Test]
        public void 索引壞了當全新的()
        {
            var fs = new MemFileSystem();
            fs.WriteText(Paths.Join(Root, "index.json"), "{not json");
            var cache = new SyncCache(fs, Root, new FakeClock(100));
            Assert.That(cache.Count, Is.EqualTo(0));
            Assert.That(cache.Put("k", Bytes(2), "h"), Is.True);
        }
    }
}
