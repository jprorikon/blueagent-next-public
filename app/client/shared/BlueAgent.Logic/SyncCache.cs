using System;
using System.Collections.Generic;
using System.Security.Cryptography;
using System.Text;

namespace BlueAgent.Logic
{
    /// <summary>
    /// App 側快取（owner 2026-10-29：「圖片、表情包和聊天記錄每次都要重新下載，能不能採用 app 側快取，
    /// 然後對比線上快取的雜湊決定是不是要重新同步」）。
    ///
    /// 一個網址（`/api/stickers/mira/smile`、`/api/media/m…`、`turns:mira:phone,api`…）＝一筆：
    /// 內容＋伺服器給的雜湊（ETag）。用法：
    /// <list type="bullet">
    /// <item>清單裡已經附了雜湊（表情包每張的 `hash`）⇒ <see cref="Fresh"/> 對上就直接用，<b>連問都不問</b>；</item>
    /// <item>不知道線上現在是什麼（聊天記錄、清單本身）⇒ 帶 <see cref="EtagOf"/> 去問（`If-None-Match`），
    ///   304 就用本機這份；</item>
    /// <item>相冊的圖：id 每次上傳都是新的、內容不會變 ⇒ 有就直接用（<see cref="Get"/>）。</item>
    /// </list>
    ///
    /// 位置按「伺服器＋身份」分（<see cref="FolderFor"/>）：換帳號不會看到別人的圖和對話。
    /// 總量有上限（<see cref="BudgetBytes"/>）：超過就從最久沒用的開始丟。
    /// 純計算＋<see cref="IFileSystem"/>：`dotnet test` 釘著（<c>SyncCacheTest</c>）。
    /// </summary>
    public sealed class SyncCache
    {
        /// <summary>預設上限 200 MB（相冊原圖一張最多 6 MB）。</summary>
        public const long DefaultBudget = 200L * 1024 * 1024;

        /// <summary>C-07：平台原圖只在本機保留七天；縮圖與持久聊天／投稿資料不受此規則影響。</summary>
        public const double OriginalMediaTtlSeconds = 7 * 24 * 60 * 60;

        private const string IndexName = "index.json";

        private sealed class Entry
        {
            public string File;
            public string Etag;
            public long Bytes;
            public double Used;
        }

        private readonly IFileSystem _fs;
        private readonly IClock _clock;
        private readonly string _root;
        private readonly Dictionary<string, Entry> _entries = new Dictionary<string, Entry>();
        private long _total;

        public long BudgetBytes { get; set; }

        /// <summary>現在存了多少位元組。</summary>
        public long TotalBytes { get { return _total; } }

        public int Count { get { return _entries.Count; } }

        public SyncCache(IFileSystem fs, string root, IClock clock, long budgetBytes = DefaultBudget)
        {
            _fs = fs;
            _root = root ?? "";
            _clock = clock;
            BudgetBytes = budgetBytes;
            Load();
        }

        /// <summary>這個「伺服器＋身份」的快取目錄名（雜湊過：目錄名不露出帳號）。</summary>
        public static string FolderFor(string baseUrl, string principal)
        {
            var server = (baseUrl ?? "").Trim().TrimEnd('/').ToLowerInvariant();
            var who = (principal ?? "").Trim();
            if (server.Length == 0 || who.Length == 0) return "";
            return Hash(server + "\n" + who).Substring(0, 16);
        }

        /// <summary>
        /// 一段內容的雜湊：跟伺服器 `blueagent_next/media/digest.py` 的 `content_tag` **同一個算法**
        /// （sha256 的前 16 個十六進位字）。回應裡沒有 `ETag`（某段代理把它丟了）時拿它頂上，
        /// 本機存的雜湊才對得上清單裡的 `hash`——不然每次打開都會當成「換過了」整包重抓。
        /// </summary>
        public static string ContentTag(byte[] data)
        {
            using (var sha = SHA256.Create())
            {
                var digest = sha.ComputeHash(data ?? new byte[0]);
                var builder = new StringBuilder(16);
                for (var i = 0; i < 8; i++) builder.Append(digest[i].ToString("x2"));
                return builder.ToString();
            }
        }

        /// <summary>伺服器回的 ETag 去掉 `W/` 與引號（存的、比的都是這個樣子）。</summary>
        public static string NormalizeEtag(string etag)
        {
            var value = (etag ?? "").Trim();
            if (value.StartsWith("W/", StringComparison.Ordinal)) value = value.Substring(2);
            return value.Trim('"');
        }

        /// <summary>存著的雜湊（沒有 ⇒ 空字串）；帶去 `If-None-Match` 用。</summary>
        public string EtagOf(string key)
        {
            return _entries.TryGetValue(key ?? "", out var entry) ? entry.Etag : "";
        }

        /// <summary>本機那份的雜湊跟線上給的一樣 ⇒ 可以直接用（雜湊是空的不算）。</summary>
        public bool Fresh(string key, string hash)
        {
            var wanted = NormalizeEtag(hash);
            return wanted.Length > 0 && EtagOf(key) == wanted;
        }

        /// <summary>讀出那一份（沒有、或檔案不見了 ⇒ null）；順便記一下「最近用過」。</summary>
        public byte[] Get(string key)
        {
            if (!_entries.TryGetValue(key ?? "", out var entry)) return null;
            if (ExpiredOriginal(key, entry))
            {
                Remove(key);                                  // 下次需要時重新向 App Server 下載
                return null;
            }
            var data = _fs.ReadBytes(Paths.Join(_root, entry.File));
            if (data == null || data.LongLength != entry.Bytes)
            {
                // 檔案被系統清掉（iOS 會清 Caches）或寫到一半 ⇒ 當沒有
                Forget(key, entry);
                Save();
                return null;
            }
            // 原圖從下載起固定保留七天，不因查看而續期；其他內容仍按最近使用做 LRU。
            if (!IsOriginalMediaKey(key)) entry.Used = _clock.NowEpochSeconds();
            return data;
        }

        public string GetText(string key)
        {
            var data = Get(key);
            return data == null ? null : Encoding.UTF8.GetString(data);
        }

        /// <summary>存一份（同一個網址會蓋掉舊的）；超出上限就從最久沒用的開始丟。</summary>
        public bool Put(string key, byte[] data, string etag)
        {
            if (string.IsNullOrEmpty(key) || data == null) return false;
            PurgeExpiredOriginals();
            if (data.LongLength > BudgetBytes) return false;     // 一份就超過上限：不存
            var file = Hash(key).Substring(0, 32) + ".bin";
            if (!_fs.WriteBytes(Paths.Join(_root, file), data)) return false;
            if (_entries.TryGetValue(key, out var old)) _total -= old.Bytes;
            _entries[key] = new Entry
            {
                File = file,
                Etag = NormalizeEtag(etag),
                Bytes = data.LongLength,
                Used = _clock.NowEpochSeconds(),
            };
            _total += data.LongLength;
            Evict(key);
            Save();
            return true;
        }

        public bool PutText(string key, string text, string etag)
        {
            return Put(key, Encoding.UTF8.GetBytes(text ?? ""), etag);
        }

        public void Remove(string key)
        {
            if (!_entries.TryGetValue(key ?? "", out var entry)) return;
            Forget(key, entry);
            _fs.Delete(Paths.Join(_root, entry.File));
            Save();
        }

        // ── 內部 ────────────────────────────────────────────────────────────

        private static bool IsOriginalMediaKey(string key)
        {
            var value = key ?? "";
            if (!value.StartsWith("/api/media/", StringComparison.Ordinal)
                && !value.StartsWith("/v1/media/", StringComparison.Ordinal)) return false;
            var query = value.IndexOf('?');
            if (query < 0) return true;
            var parts = value.Substring(query + 1).Split('&');
            foreach (var part in parts)
                if (part == "w=192" || part == "w=360") return false;
            return true;                                      // 不認得的寬度由伺服器回退原圖
        }

        private bool ExpiredOriginal(string key, Entry entry)
        {
            return IsOriginalMediaKey(key)
                && _clock.NowEpochSeconds() - entry.Used >= OriginalMediaTtlSeconds;
        }

        private void PurgeExpiredOriginals()
        {
            var expired = new List<KeyValuePair<string, Entry>>();
            foreach (var pair in _entries)
                if (ExpiredOriginal(pair.Key, pair.Value)) expired.Add(pair);
            foreach (var pair in expired)
            {
                Forget(pair.Key, pair.Value);
                _fs.Delete(Paths.Join(_root, pair.Value.File));
            }
            if (expired.Count > 0) Save();
        }

        private void Evict(string keep)
        {
            if (_total <= BudgetBytes) return;
            var order = new List<KeyValuePair<string, Entry>>(_entries);
            order.Sort((a, b) => a.Value.Used.CompareTo(b.Value.Used));
            foreach (var pair in order)
            {
                if (_total <= BudgetBytes) break;
                if (pair.Key == keep) continue;
                Forget(pair.Key, pair.Value);
                _fs.Delete(Paths.Join(_root, pair.Value.File));
            }
        }

        private void Forget(string key, Entry entry)
        {
            _entries.Remove(key);
            _total -= entry.Bytes;
        }

        private void Load()
        {
            var text = _fs.ReadText(Paths.Join(_root, IndexName));
            if (string.IsNullOrEmpty(text)) return;
            JObject index;
            try { index = new JObject(text); }
            catch (Exception) { return; }                         // 索引壞了 ⇒ 當全新的（檔案之後會被蓋掉）
            foreach (var key in index.Keys())
            {
                var row = index.OptJObject(key);
                if (row == null) continue;
                var entry = new Entry
                {
                    File = row.OptString("f"),
                    Etag = row.OptString("e"),
                    Bytes = row.OptLong("b", 0L),
                    Used = row.OptDouble("u"),
                };
                if (string.IsNullOrEmpty(entry.File) || entry.Bytes < 0) continue;
                _entries[key] = entry;
                _total += entry.Bytes;
            }
            PurgeExpiredOriginals();                         // 重開索引也執行同一條七天規則
        }

        private void Save()
        {
            var index = new JObject();
            foreach (var pair in _entries)
            {
                index.Put(pair.Key, new JObject()
                    .Put("f", pair.Value.File)
                    .Put("e", pair.Value.Etag ?? "")
                    .Put("b", pair.Value.Bytes)
                    .Put("u", pair.Value.Used));
            }
            // 先寫暫存再換名：寫到一半當掉也不會留下半個索引
            var path = Paths.Join(_root, IndexName);
            var tmp = path + ".tmp";
            if (_fs.WriteText(tmp, index.ToString())) _fs.Rename(tmp, path);
        }

        private static string Hash(string text)
        {
            using (var sha = SHA256.Create())
            {
                var digest = sha.ComputeHash(Encoding.UTF8.GetBytes(text ?? ""));
                var builder = new StringBuilder(digest.Length * 2);
                foreach (var b in digest) builder.Append(b.ToString("x2"));
                return builder.ToString();
            }
        }
    }
}
