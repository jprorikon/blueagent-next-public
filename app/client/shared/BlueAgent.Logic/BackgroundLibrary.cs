using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.RegularExpressions;

namespace BlueAgent.Logic
{
    /// <summary>
    /// 場景包（Stage backgrounds）——`blueagent/live2d/android/kmp/src/commonMain/.../BackgroundLibrary.kt`
    /// 的**逐行移植**（`BackgroundLibrary`／`BackgroundSlot`／`BackgroundRandom` 三支都在那一支 Kotlin
    /// 檔裡，所以也都在這一支 C# 檔裡）。等價性由 `fixtures/background_library.json`（Kotlin 那一側
    /// 機器產生的真值）逐條對拍。
    ///
    /// 規則（＝契約，與外殼的 Python `background_library.py` 同一份）：
    /// <list type="bullet">
    /// <item>一個包＝`backgrounds/&lt;id&gt;/` 底下有 `background.json`；**`id` 必須等於資料夾名**，否則
    /// 整包跳過；</item>
    /// <item>`image` 必填，而且是**那個資料夾裡的檔名、永遠不是路徑**；</item>
    /// <item>`thumb` 選填，缺了或是不合法就退回 `image`；</item>
    /// <item>`dim` 是主人的旋鈕（0–60）——畫面上一層藏青色的紗，從來不是藏起來的常數；</item>
    /// <item>`variants[]` 是同一幕的其他時段：`{slot,name,image,thumb}`，`slot` 必須在 <see cref="Slots"/>
    /// 裡、**重複的丟掉（不是覆蓋）**，回來的順序永遠是時段順序（dawn → night）——那就是選單畫的順序；</item>
    /// <item>**一個壞包永遠不該讓啟動失敗**——跳過它（Python 會記一行警告）。</item>
    /// </list>
    ///
    /// 背景只是裝飾：它不改角色、不改聲音、不改閘道身份。**現在是哪個時段不存在這裡**——外殼讀日本
    /// 時鐘，頁面只畫人家給它的（見 <see cref="BackgroundSlot"/>）。
    ///
    /// ⚠️ 移植陷阱（實測）：`matches(ID)` 是全比對 ⇒ C# 用 `\A…\z`（不是 `^…$`）；`sorted()` 是**序數**
    /// ⇒ `StringComparer.Ordinal`；`slot.trim().lowercase()` 的空白是 Kotlin 語意 ⇒
    /// <see cref="KotlinText.Trim"/> ＋ `ToLowerInvariant()`；`"a/b".substringAfterLast('/')` 不是
    /// 路徑守門的全部（結尾斜線 ⇒ 回空字串、也要擋）。
    /// </summary>
    public sealed class BackgroundLibrary
    {
        private readonly IAssetSource _assets;

        public BackgroundLibrary(IAssetSource assets)
        {
            _assets = assets;
        }

        /// <summary>`[a-z][a-z0-9_-]*`，**全比對**（`\A…\z` 不是 `^…$`；見 <see cref="CharacterLibrary"/> 同一條陷阱）。</summary>
        private static readonly Regex Id = new Regex("\\A[a-z][a-z0-9_-]*\\z", RegexOptions.CultureInvariant);

        /// <summary>
        /// 一個包能帶的時段，順序＝選單畫的順序（`dawn` → `night`）。清單外的時段**丟掉**、不是傳下去
        /// ——手改過的 manifest 生不出第六顆按鈕。
        /// </summary>
        public static readonly List<string> Slots = new List<string> { "dawn", "morning", "noon", "dusk", "night" };

        public List<JObject> Load()
        {
            var found = new List<JObject>();
            var dirs = _assets.List("backgrounds");
            // Kotlin 的 `sorted()`——**序數**（文化特性排序會在大小寫／重音上走鐘）。
            dirs.Sort(StringComparer.Ordinal);
            foreach (var id in dirs)
            {
                if (!Id.IsMatch(id)) continue;
                var manifest = _assets.ReadText("backgrounds/" + id + "/background.json");
                if (manifest == null) continue;
                var pack = Parse(id, manifest, name => _assets.Exists("backgrounds/" + id + "/" + name));
                if (pack == null) continue;
                found.Add(pack);
            }
            // ⚠️ 與 `CharacterLibrary.load()` 不同：這一支**不再排序**（回來的就是排序過的資料夾順序）。
            return found;
        }

        /// <summary>平台側的方便入口：載入後直接回**一整段 JSON 陣列字串**（與 <see cref="CharacterLibrary.LoadJson"/> 同一個道理）。</summary>
        public string LoadJson() => JArray.OfObjects(Load()).ToString();

        /// <summary>
        /// 讀一個 manifest。不能用的包回 `null`（Python 會丟例外、整包跳過）。
        ///
        /// ⚠️ 變體的壞掉**不是**整包跳過——只是那一格不見（Python 也是這樣）。
        /// </summary>
        public static JObject Parse(string id, string manifest, Func<string, bool> exists)
        {
            if (!Id.IsMatch(id)) return null;

            JObject raw;
            try { raw = new JObject(manifest); }
            catch { raw = null; }
            if (raw == null) return null;
            if (raw.OptString("id") != id) return null;

            // 這個資料夾裡的**檔名**、永遠不是路徑（同 CharacterLibrary 的 `inside`）。
            bool Inside(string name) => name.Length > 0 && name == AfterLastSlash(name);
            bool IsFile(string name) => Inside(name) && exists(name);

            var image = raw.OptString("image");
            if (!IsFile(image)) return null;
            var namedThumb = raw.OptString("thumb");
            var thumb = IsFile(namedThumb) ? namedThumb : image;

            return new JObject()
                .Put("id", id)
                .Put("name", Defaulted(raw.OptString("name"), id))
                .Put("tag", raw.OptString("tag"))
                .Put("image", "backgrounds/" + id + "/" + image)
                .Put("thumb", "backgrounds/" + id + "/" + thumb)
                .Put("dim", Math.Max(0, Math.Min(60, raw.OptInt("dim", 0))))
                .Put("variants", ReadVariants(id, raw, IsFile));
        }

        /// <summary>同一幕的其他時段，照 <see cref="Slots"/> 的順序。壞掉的一律丟掉。</summary>
        private static JArray ReadVariants(string id, JObject raw, Func<string, bool> isFile)
        {
            var items = raw.OptJArray("variants");
            var outVariants = new JArray();
            if (items == null) return outVariants;

            var seen = new Dictionary<string, JObject>(StringComparer.Ordinal);
            for (var index = 0; index < items.Length(); index++)
            {
                var item = items.OptJObject(index);
                if (item == null) continue;
                // `item.optString("slot").trim().lowercase()`——空白是 **Kotlin** 的語意、小寫是 ROOT。
                var slot = KotlinText.Trim(item.OptString("slot")).ToLowerInvariant();
                if (!Slots.Contains(slot) || seen.ContainsKey(slot)) continue;
                var image = item.OptString("image");
                if (!isFile(image)) continue;
                var namedThumb = item.OptString("thumb");
                var thumb = isFile(namedThumb) ? namedThumb : image;
                seen[slot] = new JObject()
                    .Put("slot", slot)
                    .Put("name", Defaulted(item.OptString("name"), slot))
                    .Put("image", "backgrounds/" + id + "/" + image)
                    .Put("thumb", "backgrounds/" + id + "/" + thumb);
            }

            foreach (var slot in Slots)
            {
                if (seen.TryGetValue(slot, out var v)) outVariants.Put(v);
            }
            return outVariants;
        }

        /// <summary>`String.ifEmpty { fallback }`——**只有空字串**換掉（`" "` 不是空的）。</summary>
        private static string Defaulted(string value, string fallback) => value.Length == 0 ? fallback : value;

        /// <summary>`String.substringAfterLast('/')`（同 <see cref="CharacterLibrary"/> 的那一支）。</summary>
        private static string AfterLastSlash(string s)
        {
            var i = s.LastIndexOf('/');
            return i < 0 ? s : s.Substring(i + 1);
        }
    }

    /// <summary>
    /// 時段，由**外殼**決定、永遠不由頁面決定（交接筆記 §1.3：「切點寫在殼裡」）。
    ///
    /// Python 外殼的 `SLOT_HOURS` 是 05-08 dawn／08-11 morning／11-16 noon／16-19 dusk／19-05 night，
    /// 讀日本時鐘。UI 只送場景（`set_background(id)` 只有一個參數）——時段跟著外殼手上那個走。
    /// </summary>
    public static class BackgroundSlot
    {
        /// <summary>小時 ⇒ 時段（`((hour % 24) + 24) % 24`：負數與跨日都算得對）。</summary>
        public static string ForHour(int hour)
        {
            var h = ((hour % 24) + 24) % 24;
            if (h >= 5 && h <= 7) return "dawn";
            if (h >= 8 && h <= 10) return "morning";
            if (h >= 11 && h <= 15) return "noon";
            if (h >= 16 && h <= 18) return "dusk";
            return "night";
        }
    }

    /// <summary>
    /// 隨機模式（owner 2026-10-06：「背景更換添加隨機模式，隨機模式下每次時間段交換點時更換時間同時更換
    /// 場景（不包含無背景）」）。
    ///
    /// 規則（三個都是刻意的）：
    /// <list type="bullet">
    /// <item>**池子＝場景包**；`''`（無背景）**不是**候選——隨機不會把畫面變回斜線紋底；</item>
    /// <item>**排除現在這一個**：切點上「換場景」要是看得見的變化；抽到同一個等於沒換。池子被排除到空
    /// （只有一個場景包，或那一個就是現在用的）⇒ 回現在的，什麼都不動；</item>
    /// <item>`roll` 由呼叫端給（[0,1)），這一支是純函式 ⇒ 離線可驗（見 `BackgroundRandomTest`）。</item>
    /// </list>
    ///
    /// ⚠️ 移植陷阱：比較的是**未 trim 的 `current`**（池子裡是 trim 過的）——`" school "` 與 `"school"`
    /// 在這一支眼裡**不是同一個**。照抄，不要「順手修好」。
    /// </summary>
    public static class BackgroundRandom
    {
        /// <summary>
        /// 抽下一個場景 id。`roll` 是 [0,1) 的亂數；越界／NaN 一律當 0。
        ///
        /// ⚠️ `Double.isFinite()` 才走 `((roll % 1.0) + 1.0) % 1.0`；NaN／±Inf ⇒ 0.0
        /// （`%` 對負數是**取餘**、不是算術模——`-0.25` ⇒ `0.75`，這條也被 fixture 釘住）。
        /// </summary>
        public static string Pick(List<string> ids, string current, double roll)
        {
            // `ids.map { it.trim() }.filter { it.isNotEmpty() }.distinct().filter { it != current }`
            // ——`distinct()` 保序（第一次出現的位置留下）。
            var pool = new List<string>();
            var seen = new HashSet<string>(StringComparer.Ordinal);
            foreach (var value in ids)
            {
                var trimmed = KotlinText.Trim(value);
                if (trimmed.Length == 0) continue;
                if (!seen.Add(trimmed)) continue;
                if (trimmed != current) pool.Add(trimmed);
            }
            if (pool.Count == 0) return current;

            var finite = !double.IsNaN(roll) && !double.IsInfinity(roll);
            var r = finite ? ((roll % 1.0) + 1.0) % 1.0 : 0.0;
            var index = (int)(r * pool.Count);
            if (index < 0) index = 0;
            if (index > pool.Count - 1) index = pool.Count - 1;
            return pool[index];
        }
    }
}
