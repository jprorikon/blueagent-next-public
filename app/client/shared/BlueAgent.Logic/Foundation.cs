using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Threading;

namespace BlueAgent.Logic
{
    /// <summary>
    /// **共用邏輯層用到的平台縫（foundation）**——C# 版，對照 Kotlin 的 `Fs.kt`。
    ///
    /// 設計原則（照抄 Kotlin 那側，理由一字不變）：
    ///  * **不做全域狀態**：需要平台東西的類別一律由呼叫端注入（檔案系統／資產來源／設定後端／時鐘）
    ///    ⇒ 單測可以餵一個記憶體實作或暫存目錄，不依賴裝置。
    ///  * 平台縫 **放在介面後面**（C# 沒有 `expect/actual`，介面＋各平台一份實作是同一個意思）：
    ///    集成層（Unity 插件）提供 `IFileSystem`／`ISettingsBackend`／`IClock` 的真實作。
    ///  * 路徑一律用**絕對路徑字串**，不引進平台型別。
    ///
    /// ⚠️ 這一支是**契約**：要改介面形狀＝改兩邊（這是刻意的，介面被改動時編譯會全部指出來）。
    /// </summary>
    public interface IFileSystem
    {
        /// <summary>讀整個檔（UTF-8）。不存在／讀不到 ⇒ null（呼叫端不必 try）。</summary>
        string ReadText(string path);

        /// <summary>覆寫寫入（UTF-8），目錄不存在就建。成功回 true。</summary>
        bool WriteText(string path, string text);

        /// <summary>追加（UTF-8），目錄不存在就建。成功回 true。</summary>
        bool AppendText(string path, string text);

        bool Exists(string path);

        /// <summary>目錄下的**檔名**（不遞迴、不含路徑）；不存在／不是目錄 ⇒ 空清單。</summary>
        List<string> List(string dir);

        /// <summary>建目錄（含上層）。</summary>
        bool Mkdirs(string path);

        /// <summary>改名／搬移（原子上稿那條路要用）。</summary>
        bool Rename(string from, string to);

        /// <summary>讀整個檔的二進位內容（模型 blob 用）。不存在／讀不到 ⇒ null。</summary>
        byte[] ReadBytes(string path);

        /// <summary>覆寫寫入二進位（目錄不存在就建）。成功回 true。</summary>
        bool WriteBytes(string path, byte[] bytes);

        bool Delete(string path);
    }

    /// <summary>路徑工具——`pathJoin`：空片段略過、各段去尾斜線、全空回 `/`。</summary>
    public static class Paths
    {
        public static string Join(params string[] parts)
        {
            var kept = new List<string>();
            foreach (var p in parts)
            {
                if (!string.IsNullOrEmpty(p)) kept.Add(p.TrimEnd('/'));
            }
            // 照 Kotlin 寫：`filter{isNotEmpty}.join{trimEnd('/')}.ifEmpty { "/" }`——
            // **判斷「空」是在接完之後**（所以 `pathJoin("/")` ⇒ `""` ⇒ `"/"`）。
            var joined = string.Join("/", kept);
            return joined.Length == 0 ? "/" : joined;
        }
    }

    /// <summary>
    /// 「去哪裡拿 app 資產」——安卓是 APK 裡的 `assets/`、iOS 是 bundle 裡的一棵目錄、單測是磁碟目錄。
    /// 三個動作就是原本那些檔案需要的全部。
    /// </summary>
    public interface IAssetSource
    {
        /// <summary>目錄下的檔名（不遞迴）。</summary>
        List<string> List(string dir);

        /// <summary>讀一個資產（UTF-8）；不存在 ⇒ null。</summary>
        string ReadText(string path);

        bool Exists(string path);
    }

    /// <summary>以「磁碟上一棵目錄」為底的資產來源（單測／bundle 快照都用它）。</summary>
    public sealed class DirAssetSource : IAssetSource
    {
        private readonly string _root;
        private readonly IFileSystem _fs;

        public DirAssetSource(string root, IFileSystem fs)
        {
            _root = root;
            _fs = fs;
        }

        public List<string> List(string dir) => _fs.List(Paths.Join(_root, dir));
        public string ReadText(string path) => _fs.ReadText(Paths.Join(_root, path));
        public bool Exists(string path) => _fs.Exists(Paths.Join(_root, path));
    }

    /// <summary>
    /// 設定的儲存後端（鍵值）——安卓是 `SharedPreferences`、iOS 是 `UserDefaults`。
    ///
    /// ⚠️ **刻意保留原生型別**（string／int／bool／float），不做成「一律存字串」：兩邊的後端本來就
    /// 分型別，而**已經存在使用者手機上的值也是那個型別**——改成純字串會讓既有設定讀不出來
    /// （`GetBoolean` 對一個字串回預設值），等於把他設定頁裡的東西清掉。
    ///
    /// **鍵的名字、預設值、夾界、遷移規則都在共用邏輯那一側**，平台這層不該知道任何一個鍵的意義。
    /// </summary>
    public interface ISettingsBackend
    {
        string GetString(string key, string def);
        int GetInt(string key, int def);
        bool GetBoolean(string key, bool def);
        float GetFloat(string key, float def);

        void PutString(string key, string value);
        void PutInt(string key, int value);
        void PutBoolean(string key, bool value);
        void PutFloat(string key, float value);

        void Remove(string key);

        /// <summary>這個鍵有沒有值（遷移判斷用：舊鍵名還在不在）。</summary>
        bool Contains(string key);

        List<string> Keys();
    }

    /// <summary>純記憶體的後端：單測用（也讓「沒有平台」的環境能跑整套邏輯）。</summary>
    public sealed class MapSettingsBackend : ISettingsBackend
    {
        // 插入序 ＋ 同名鍵留在原位（＝ LinkedHashMap；Kotlin 的 `keys()` 就是回這個順序）
        private readonly List<string> _order = new List<string>();
        private readonly Dictionary<string, object> _map = new Dictionary<string, object>();

        public MapSettingsBackend() { }

        /// <summary>種子值（單測：把「使用者手機上已有的設定」餵進來）。</summary>
        public MapSettingsBackend(IEnumerable<KeyValuePair<string, object>> seed)
        {
            foreach (var kv in seed) Set(kv.Key, kv.Value);
        }

        /// <summary>Kotlin 的 `as? Number`——**只有數值型別算**（bool／char／DateTime 都不算，會回預設）。</summary>
        private static bool IsNumber(object v) =>
            v is int || v is long || v is short || v is sbyte || v is byte ||
            v is ushort || v is uint || v is ulong || v is float || v is double || v is decimal;

        public string GetString(string key, string def) => _map.TryGetValue(key, out var v) && v is string s ? s : def;

        /// <summary>`Number.toInt()`：浮點**往零截斷**（不是四捨五入）。</summary>
        public int GetInt(string key, int def) => _map.TryGetValue(key, out var v) && IsNumber(v) ? ToInt(v) : def;

        public bool GetBoolean(string key, bool def) => _map.TryGetValue(key, out var v) && v is bool b ? b : def;

        /// <summary>`Number.toFloat()`（整數值轉成浮點）。</summary>
        public float GetFloat(string key, float def) => _map.TryGetValue(key, out var v) && IsNumber(v) ? ToFloat(v) : def;

        private static int ToInt(object v) => v is int i ? i
            : v is long l ? unchecked((int)l)
            : v is short sh ? sh
            : v is sbyte sb ? sb
            : v is byte by ? by
            : v is ushort us ? us
            : v is uint ui ? unchecked((int)ui)
            : v is ulong ul ? unchecked((int)ul)
            : v is float f ? (int)f
            : v is double d ? (int)d
            : v is decimal m ? (int)m
            : 0;

        private static float ToFloat(object v) => v is float f ? f
            : v is double d ? (float)d
            : v is decimal m ? (float)m
            : (float)Convert.ToInt64(v, System.Globalization.CultureInfo.InvariantCulture);

        /// <summary>同名鍵＝取代但**留在原位**（與 `JObject.Set` 同一條規則）。</summary>
        private void Set(string key, object value)
        {
            if (!_map.ContainsKey(key)) _order.Add(key);
            _map[key] = value;
        }

        public void PutString(string key, string value) => Set(key, value);
        public void PutInt(string key, int value) => Set(key, value);
        public void PutBoolean(string key, bool value) => Set(key, value);
        public void PutFloat(string key, float value) => Set(key, value);

        public void Remove(string key)
        {
            if (_map.Remove(key)) _order.Remove(key);
        }

        public bool Contains(string key) => _map.ContainsKey(key);

        public List<string> Keys() => new List<string>(_order);
    }

    /// <summary>
    /// 兩端各一個實作的最小鎖（common 沒有 `ReentrantLock`）。
    ///
    /// 為什麼需要：`HistoryStore` 的改檔（append／rewrite）會被不同執行緒叫到（殼的 worker、橋回呼；
    /// iOS 是主執行緒與音訊回呼）——拿掉鎖就是「兩邊同時改同一個檔」的競態。
    /// C# 用 `Monitor`（**本身就可重入**，與 JVM 的 `ReentrantLock`／iOS 的 `NSRecursiveLock` 相同語意；
    /// 寫入路徑裡會再進鎖，例如 append 內部叫 rewrite）。
    /// </summary>
    public sealed class RecursiveLock
    {
        private readonly object _gate = new object();

        public void Lock() => Monitor.Enter(_gate);
        public void Unlock() => Monitor.Exit(_gate);

        /// <summary>進鎖跑一段（用完一定放掉）。取名 `Guard` 對照 Kotlin 的 `Lock.guard`。</summary>
        public T Guard<T>(Func<T> block)
        {
            Lock();
            try { return block(); }
            finally { Unlock(); }
        }

        public void Guard(Action block)
        {
            Lock();
            try { block(); }
            finally { Unlock(); }
        }
    }

    /// <summary>
    /// 時間縫。Kotlin 那側是兩支 `expect fun`（`nowEpochSeconds()`／`monotonicMs()`），
    /// 這裡收成一個介面：**要對拍的東西不能直接讀系統時鐘**（fixture 沒法重現），所以由呼叫端注入。
    ///
    /// * <see cref="NowEpochSeconds"/>：掛鐘秒（`history.jsonl` 的 `ts` 欄位）。
    /// * <see cref="MonotonicMs"/>：**單調**毫秒（不受 NTP／使用者改時間影響）——冷卻窗、閘門、打分耗時靠它。
    ///   為什麼不共用上面那個：掛鐘被往回拉會讓冷卻窗變成負的（一整段時間不再觸發）。
    /// </summary>
    public interface IClock
    {
        double NowEpochSeconds();
        long MonotonicMs();
    }

    /// <summary>真實時鐘（集成層用）。</summary>
    public sealed class SystemClock : IClock
    {
        private readonly DateTime _epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);
        private readonly System.Diagnostics.Stopwatch _since = System.Diagnostics.Stopwatch.StartNew();

        public double NowEpochSeconds() => (DateTime.UtcNow - _epoch).TotalSeconds;

        public long MonotonicMs() => _since.ElapsedMilliseconds;
    }

    /// <summary>測試用時鐘：時間由測試推進（fixture 重放靠它）。</summary>
    public sealed class FakeClock : IClock
    {
        private double _epoch;
        private long _mono;

        public FakeClock(double epochSeconds = 0.0, long monotonicMs = 0L)
        {
            _epoch = epochSeconds;
            _mono = monotonicMs;
        }

        public double NowEpochSeconds() => _epoch;
        public long MonotonicMs() => _mono;

        public void Advance(double seconds, long ms)
        {
            _epoch += seconds;
            _mono += ms;
        }
    }

    /// <summary>真實檔案系統（集成層用；單測不要用它——用記憶體實作才可重現）。</summary>
    public sealed class SystemFileSystem : IFileSystem
    {
        public string ReadText(string path)
        {
            try { return File.Exists(path) ? File.ReadAllText(path, Encoding.UTF8) : null; }
            catch { return null; }
        }

        public bool WriteText(string path, string text)
        {
            try
            {
                var dir = Path.GetDirectoryName(path);
                if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
                File.WriteAllText(path, text, new UTF8Encoding(false));
                return true;
            }
            catch { return false; }
        }

        public bool AppendText(string path, string text)
        {
            try
            {
                var dir = Path.GetDirectoryName(path);
                if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
                File.AppendAllText(path, text, new UTF8Encoding(false));
                return true;
            }
            catch { return false; }
        }

        /// <summary>檔案或**目錄**存在都算（對齊 Kotlin 的 `Fs.exists`）。</summary>
        public bool Exists(string path) => File.Exists(path) || Directory.Exists(path);

        public List<string> List(string dir)
        {
            var outp = new List<string>();
            try
            {
                if (!Directory.Exists(dir)) return outp;
                // Kotlin 的 `File.list()` 回**檔案與子目錄**兩者（「列出角色包目錄」那類模組靠這條）。
                foreach (var e in Directory.GetFileSystemEntries(dir)) outp.Add(Path.GetFileName(e));
            }
            catch { /* 讀不到＝空清單，與 Kotlin 那側一致 */ }
            return outp;
        }

        public bool Mkdirs(string path)
        {
            try { Directory.CreateDirectory(path); return true; }
            catch { return false; }
        }

        public bool Rename(string from, string to)
        {
            try
            {
                if (File.Exists(to)) File.Delete(to);
                File.Move(from, to);
                return true;
            }
            catch { return false; }
        }

        public byte[] ReadBytes(string path)
        {
            try { return File.Exists(path) ? File.ReadAllBytes(path) : null; }
            catch { return null; }
        }

        public bool WriteBytes(string path, byte[] bytes)
        {
            try
            {
                var dir = Path.GetDirectoryName(path);
                if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
                File.WriteAllBytes(path, bytes);
                return true;
            }
            catch { return false; }
        }

        public bool Delete(string path)
        {
            try
            {
                if (File.Exists(path)) { File.Delete(path); return true; }
                return false;
            }
            catch { return false; }
        }
    }
}
