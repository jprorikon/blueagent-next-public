using System;
using System.Collections.Generic;
using System.Text;

namespace BlueAgent.Logic
{
    /// <summary>
    /// **純記憶體的檔案系統**——單測／fixture 對拍用（在共用層，與 Kotlin 那側把 `MapSettingsBackend`
    /// 放 `commonMain` 是同一個思路：讓「沒有平台」的環境也能跑整套邏輯）。
    ///
    /// 語意刻意對齊 `Fs`：讀不到 ⇒ null（不丟）、寫入會自動建目錄、`List` 只回**該層**的名字
    /// **且包含子目錄名**（Kotlin 的 `File.list()` 就是檔案＋目錄一起回——`CharacterLibrary` 這類
    /// 「列出角色包目錄」的模組靠這條，漏了就整支對不上）。
    /// </summary>
    public sealed class MemFileSystem : IFileSystem
    {
        private readonly Dictionary<string, byte[]> _files = new Dictionary<string, byte[]>();
        private readonly HashSet<string> _dirs = new HashSet<string>();

        public MemFileSystem() { }

        public string ReadText(string path)
        {
            var b = ReadBytes(path);
            return b == null ? null : Encoding.UTF8.GetString(b);
        }

        public bool WriteText(string path, string text) => WriteBytes(path, Encoding.UTF8.GetBytes(text ?? string.Empty));

        public bool AppendText(string path, string text)
        {
            var existing = ReadText(path) ?? string.Empty;
            return WriteText(path, existing + (text ?? string.Empty));
        }

        /// <summary>檔案或**目錄**存在都算（對齊 Kotlin 的 `Fs.exists`）。</summary>
        public bool Exists(string path) => _files.ContainsKey(path) || _dirs.Contains(path);

        public List<string> List(string dir)
        {
            var outp = new List<string>();
            var prefix = dir.TrimEnd('/') + "/";

            foreach (var key in _files.Keys)
            {
                var rest = DirectChild(prefix, key);
                if (rest != null && !outp.Contains(rest)) outp.Add(rest);
            }
            foreach (var d in _dirs)
            {
                var rest = DirectChild(prefix, d);
                if (rest != null && !outp.Contains(rest)) outp.Add(rest);
            }
            return outp;
        }

        /// <summary>`path` 是不是 `prefix` 的**直接**子項；是就回它的名字，否則 null（遞迴項不算）。</summary>
        private static string DirectChild(string prefix, string path)
        {
            if (!path.StartsWith(prefix, StringComparison.Ordinal)) return null;
            var rest = path.Substring(prefix.Length);
            return rest.Length > 0 && rest.IndexOf('/') < 0 ? rest : null;
        }

        public bool Mkdirs(string path)
        {
            var p = path.TrimEnd('/');
            while (!string.IsNullOrEmpty(p) && p != "/")
            {
                _dirs.Add(p);
                var idx = p.LastIndexOf('/');
                p = idx <= 0 ? null : p.Substring(0, idx);
            }
            return true;
        }

        public bool Rename(string from, string to)
        {
            if (!_files.TryGetValue(from, out var b)) return false;
            _files.Remove(from);
            _files[to] = b;
            EnsureParents(to);
            return true;
        }

        public byte[] ReadBytes(string path) => _files.TryGetValue(path, out var b) ? (byte[])b.Clone() : null;

        public bool WriteBytes(string path, byte[] bytes)
        {
            _files[path] = (byte[])bytes.Clone();
            EnsureParents(path);
            return true;
        }

        public bool Delete(string path)
        {
            if (_files.Remove(path)) return true;
            return _dirs.Remove(path);
        }

        /// <summary>寫檔時把上層目錄記下來（真實 fs 也會自動建目錄——不記的話 `List` 少了目錄名）。</summary>
        private void EnsureParents(string path)
        {
            var idx = path.LastIndexOf('/');
            if (idx > 0) Mkdirs(path.Substring(0, idx));
        }

        /// <summary>測試方便：直接看一眼檔數（不經過 <see cref="ReadText"/> 的 null 語意）。</summary>
        public int Count => _files.Count;
    }
}
