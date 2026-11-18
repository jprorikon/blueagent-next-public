using System;
using System.Collections.Generic;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace BlueAgent.Logic
{
    /// <summary>SQLite 原生錯誤；SQL 與參數不得攔截成靜默成功。</summary>
    public sealed class SqliteException : Exception
    {
        public int ResultCode { get; private set; }

        internal SqliteException(int code, string message) : base(message)
        {
            ResultCode = code;
        }
    }

    /// <summary>
    /// 小型 SQLite 驅動：iOS／Android／macOS 使用系統 sqlite3，不需要把聊天或投稿序列化成 JSON 檔冒充資料庫。
    /// 同一實例的所有操作串行；交易由 Repository 使用，查詢結果在 Statement 關閉前複製出來。
    /// </summary>
    public sealed class SqliteDb : IDisposable
    {
        public sealed class Row
        {
            private readonly Dictionary<string, object> _values;

            internal Row(Dictionary<string, object> values) { _values = values; }

            public string String(string name)
            {
                return _values.TryGetValue(name, out var value) && value != null ? (string)value : "";
            }

            public long Long(string name)
            {
                return _values.TryGetValue(name, out var value) && value != null
                    ? Convert.ToInt64(value, CultureInfo.InvariantCulture) : 0L;
            }

            public double Double(string name)
            {
                return _values.TryGetValue(name, out var value) && value != null
                    ? Convert.ToDouble(value, CultureInfo.InvariantCulture) : 0.0;
            }
        }

        private readonly object _gate = new object();
        private IntPtr _db;

        public SqliteDb(string path)
        {
            if (string.IsNullOrEmpty(path)) throw new ArgumentException("SQLite path is empty", "path");
            var directory = System.IO.Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(directory)) System.IO.Directory.CreateDirectory(directory);
            Check(Native.sqlite3_open_v2(path, out _db, Native.ReadWrite | Native.Create, IntPtr.Zero));
            try
            {
                Check(Native.sqlite3_busy_timeout(_db, 5000));
                Execute("PRAGMA foreign_keys=ON");
                Execute("PRAGMA journal_mode=WAL");
            }
            catch
            {
                Dispose();
                throw;
            }
        }

        public void Execute(string sql, params object[] args)
        {
            lock (_gate)
            {
                var statement = Prepare(sql, args);
                try
                {
                    var code = Native.sqlite3_step(statement);
                    if (code != Native.Done && code != Native.Row) Throw(code);
                }
                finally { Native.sqlite3_finalize(statement); }
            }
        }

        public List<Row> Query(string sql, params object[] args)
        {
            lock (_gate)
            {
                var statement = Prepare(sql, args);
                try
                {
                    var rows = new List<Row>();
                    while (true)
                    {
                        var code = Native.sqlite3_step(statement);
                        if (code == Native.Done) return rows;
                        if (code != Native.Row) Throw(code);
                        var values = new Dictionary<string, object>(StringComparer.Ordinal);
                        var count = Native.sqlite3_column_count(statement);
                        for (var i = 0; i < count; i++)
                        {
                            var name = Native.Utf8(Native.sqlite3_column_name(statement, i));
                            values[name] = Value(statement, i);
                        }
                        rows.Add(new Row(values));
                    }
                }
                finally { Native.sqlite3_finalize(statement); }
            }
        }

        public long ScalarLong(string sql, params object[] args)
        {
            var rows = Query(sql, args);
            if (rows.Count == 0) return 0L;
            return rows[0].Long("value");
        }

        public void Transaction(Action action)
        {
            if (action == null) return;
            lock (_gate)
            {
                Execute("BEGIN IMMEDIATE");
                try
                {
                    action();
                    Execute("COMMIT");
                }
                catch
                {
                    try { Execute("ROLLBACK"); } catch (Exception) { }
                    throw;
                }
            }
        }

        public void Dispose()
        {
            lock (_gate)
            {
                if (_db == IntPtr.Zero) return;
                Native.sqlite3_close_v2(_db);
                _db = IntPtr.Zero;
            }
        }

        private IntPtr Prepare(string sql, object[] args)
        {
            if (_db == IntPtr.Zero) throw new ObjectDisposedException("SqliteDb");
            var bytes = Encoding.UTF8.GetBytes(sql ?? "");
            Check(Native.sqlite3_prepare_v2(_db, bytes, bytes.Length, out var statement, IntPtr.Zero));
            try
            {
                for (var i = 0; i < (args == null ? 0 : args.Length); i++) Bind(statement, i + 1, args[i]);
                return statement;
            }
            catch
            {
                Native.sqlite3_finalize(statement);
                throw;
            }
        }

        private void Bind(IntPtr statement, int index, object value)
        {
            int code;
            if (value == null) code = Native.sqlite3_bind_null(statement, index);
            else if (value is string text)
            {
                var bytes = Encoding.UTF8.GetBytes(text);
                code = Native.sqlite3_bind_text(statement, index, bytes, bytes.Length, Native.Transient);
            }
            else if (value is bool boolean) code = Native.sqlite3_bind_int64(statement, index, boolean ? 1 : 0);
            else if (value is int || value is long || value is short || value is byte)
                code = Native.sqlite3_bind_int64(statement, index, Convert.ToInt64(value, CultureInfo.InvariantCulture));
            else if (value is double || value is float)
                code = Native.sqlite3_bind_double(statement, index, Convert.ToDouble(value, CultureInfo.InvariantCulture));
            else throw new ArgumentException("Unsupported SQLite value: " + value.GetType().FullName);
            Check(code);
        }

        private static object Value(IntPtr statement, int column)
        {
            switch (Native.sqlite3_column_type(statement, column))
            {
                case Native.Null: return null;
                case Native.Integer: return Native.sqlite3_column_int64(statement, column);
                case Native.Float: return Native.sqlite3_column_double(statement, column);
                default:
                    var pointer = Native.sqlite3_column_text(statement, column);
                    var length = Native.sqlite3_column_bytes(statement, column);
                    if (pointer == IntPtr.Zero || length == 0) return "";
                    var bytes = new byte[length];
                    Marshal.Copy(pointer, bytes, 0, length);
                    return Encoding.UTF8.GetString(bytes);
            }
        }

        private void Check(int code)
        {
            if (code != Native.Ok) Throw(code);
        }

        private void Throw(int code)
        {
            var detail = _db == IntPtr.Zero ? "sqlite closed" : Native.Utf8(Native.sqlite3_errmsg(_db));
            throw new SqliteException(code, "sqlite " + code + ": " + detail);
        }

        private static class Native
        {
            /// <summary>
            /// 原生庫名：iOS／macOS 由系統提供 `libsqlite3`，`DllImport("sqlite3")` 就能解析。
            /// **Android 沒有系統庫**（只有非公開的 `libsqlite.so`，API 24+ 的 linker 不讓 app 載入），
            /// 而 IL2CPP 的動態載入在 Android 上只按「可執行檔所在目錄」解析名字（實測會去找
            /// `/system/bin/sqlite3`、`/system/bin/libsqlite3.so`，兩者都不存在），
            /// 即使 APK 內帶了 `lib/arm64-v8a/libsqlite3.so` 也載不到。
            /// ⇒ Android 走**靜態庫**：`Assets/Plugins/Android/libsqlite3.a` 由 Unity 直接鏈進
            /// libil2cpp，符號在主映像裡，用 `__Internal` 解析（本專案其他原生插件同一個慣例）。
            /// </summary>
#if UNITY_ANDROID && !UNITY_EDITOR
            internal const string Library = "__Internal";
#else
            internal const string Library = "sqlite3";
#endif

            internal const int Ok = 0;
            internal const int Row = 100;
            internal const int Done = 101;
            internal const int Integer = 1;
            internal const int Float = 2;
            internal const int Null = 5;
            internal const int ReadWrite = 0x2;
            internal const int Create = 0x4;
            internal static readonly IntPtr Transient = new IntPtr(-1);

            internal static string Utf8(IntPtr pointer)
            {
                return pointer == IntPtr.Zero ? "" : Marshal.PtrToStringAnsi(pointer);
            }

            [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
            internal static extern int sqlite3_open_v2(string path, out IntPtr db, int flags, IntPtr vfs);
            [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
            internal static extern int sqlite3_close_v2(IntPtr db);
            [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
            internal static extern int sqlite3_busy_timeout(IntPtr db, int milliseconds);
            [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
            internal static extern IntPtr sqlite3_errmsg(IntPtr db);
            [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
            internal static extern int sqlite3_prepare_v2(IntPtr db, byte[] sql, int bytes,
                out IntPtr statement, IntPtr tail);
            [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
            internal static extern int sqlite3_step(IntPtr statement);
            [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
            internal static extern int sqlite3_finalize(IntPtr statement);
            [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
            internal static extern int sqlite3_bind_null(IntPtr statement, int index);
            [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
            internal static extern int sqlite3_bind_int64(IntPtr statement, int index, long value);
            [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
            internal static extern int sqlite3_bind_double(IntPtr statement, int index, double value);
            [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
            internal static extern int sqlite3_bind_text(IntPtr statement, int index, byte[] value,
                int bytes, IntPtr destructor);
            [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
            internal static extern int sqlite3_column_count(IntPtr statement);
            [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
            internal static extern IntPtr sqlite3_column_name(IntPtr statement, int column);
            [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
            internal static extern int sqlite3_column_type(IntPtr statement, int column);
            [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
            internal static extern long sqlite3_column_int64(IntPtr statement, int column);
            [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
            internal static extern double sqlite3_column_double(IntPtr statement, int column);
            [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
            internal static extern IntPtr sqlite3_column_text(IntPtr statement, int column);
            [DllImport(Library, CallingConvention = CallingConvention.Cdecl)]
            internal static extern int sqlite3_column_bytes(IntPtr statement, int column);
        }
    }
}
