using System;
using System.Collections.Generic;
using BlueAgent.Logic;
using BlueAgent.Screens;
using UnityEngine;
using UnityEngine.Networking;
using UnityEngine.UI;

namespace BlueAgent.Next
{
    /// <summary>
    /// 表情包：聊天頁打字列旁那顆笑臉打開的**表情盤**，以及氣泡裡的那張圖。
    ///
    /// C-17：素材唯一真源是 `app/client/assets/stickers/`，清單與圖都由 App Server
    /// `/v1/stickers` 提供；Agent 只傳 id／meaning。端末不打包，換素材不必重出包。
    /// </summary>
    public sealed class NextStickers
    {
        private const float TrayHeight = 196f;
        private const float Cell = 72f;
        /// <summary>盤子裡那一格要的縮圖寬（伺服器 `media/thumbs.py` 只收固定幾個寬）。</summary>
        private const int TrayThumb = 192;
        /// <summary>格與格之間的間距（<see cref="PinGridHeight"/> 算高度時要用同一個值）。</summary>
        private const float Spacing = 10f;
        /// <summary>一列幾格（<see cref="PinGridHeight"/> 算行數時要用同一個值）。</summary>
        private const int Columns = 5;

        /// <summary>url → PNG 位元組（整個 app 共用；表情包很小、也不敏感）。</summary>
        private static readonly Dictionary<string, byte[]> Bytes = new Dictionary<string, byte[]>();

        private NextShell _shell;
        private Action<string> _pick;
        private RectTransform _tray;
        private LayoutElement _trayHeight;
        private CanvasGroup _trayGroup;
        private RectTransform _grid;
        private RectTransform _content;
        private ScrollRect _scroll;
        private Component _hint;
        private string _loadedFor = "";
        private bool _loading;

        public bool IsOpen { get; private set; }

        /// <summary>App Server 上那張圖的路徑（歷史裡只記 id 的那幾種用它拼）。</summary>
        public static string UrlOf(string entity, string id)
        {
            return "/v1/stickers/" + UnityWebRequest.EscapeURL(entity ?? "") + "/" + UnityWebRequest.EscapeURL(id ?? "");
        }

        /// <summary>
        /// 把一張表情畫進 <paramref name="image"/>（抓不到就留底色；回來前 image 被銷毀就什麼都不做）。
        /// <paramref name="hash"/>＝清單給的這張的雜湊（氣泡裡的沒有 ⇒ 空字串）：
        /// 跟本機快取的一樣 ⇒ 不發請求（owner 2026-10-29「對比線上的雜湊決定要不要重新同步」）；
        /// 不一樣 ⇒ 圖換過了，記憶體裡那份也作廢、重抓。
        /// </summary>
        public static void Paint(NextApi api, Image image, string url, string hash = "")
        {
            if (image == null || string.IsNullOrEmpty(url)) return;
            var stale = !string.IsNullOrEmpty(hash) && api.Cache != null && !api.Cache.Fresh(url, hash);
            if (stale) Bytes.Remove(url);
            else if (Bytes.TryGetValue(url, out var cached)) { Apply(image, cached); return; }
            var disk = stale ? null : api.CachedBytes(url, hash);
            if (disk != null)
            {
                Bytes[url] = disk;
                Apply(image, disk);
                return;
            }
            Enqueue(api, image, url);
        }

        // ── 下載佇列（**不可同時開 104 個**）────────────────────────────────────
        //
        // ⚠️ 2026-10-29 真機實測：一次性對 104 張圖各自發一個 `UnityWebRequest`，
        // 結果只有 4 張回來，其餘的連回應都沒有（`LoadImage failed: bytes=49152`、
        // 大部分 `GetBytes` 的回呼根本沒進）⇒ 連線/時間outs 被打爆。
        // ⇒ **限制同時在飛的請求數**，其餘排隊。

        /// <summary>同時在飛的上限（實測 6 穩；再多就會開始掉回應）。</summary>
        private const int MaxConcurrent = 6;

        private sealed class Pending
        {
            public NextApi Api;
            public Image Image;
            public string Url;
        }

        private static readonly Queue<Pending> Queue = new Queue<Pending>();
        private static int _inFlight;
        /// <summary>佇列屬於哪一個工作階段（<see cref="NextApi.Session"/> 的 epoch）。</summary>
        private static int _queueEpoch = -1;

        private static void Enqueue(NextApi api, Image image, string url)
        {
            // ⚠️ 換了工作階段（登出／token 失效）⇒ 舊的那幾個請求的回呼會被 `Settle` 丟掉、
            // 永遠不會回來 ⇒ `_inFlight` 減不回去，佔滿 6 個名額之後整條佇列就再也不動了。
            // ⇒ 換階段就整條重來（舊頁面的格子也早就拆了）。
            var epoch = api.Session.Epoch;
            if (epoch != _queueEpoch)
            {
                Queue.Clear();
                _inFlight = 0;
                _queueEpoch = epoch;
            }
            Queue.Enqueue(new Pending { Api = api, Image = image, Url = url });
            Pump();
        }

        private static void Pump()
        {
            while (_inFlight < MaxConcurrent && Queue.Count > 0)
            {
                var item = Queue.Dequeue();
                if (item.Image == null || string.IsNullOrEmpty(item.Url)) continue;   // 頁面早換了
                _inFlight++;
                var epoch = _queueEpoch;
                item.Api.GetBytes(item.Url, data =>
                {
                    if (epoch != _queueEpoch) return;   // 上一個工作階段的（名額已經整條歸零過了）
                    _inFlight--;
                    if (data != null && data.Length > 0)
                    {
                        Bytes[item.Url] = data;
                        Apply(item.Image, data);
                    }
                    Pump();
                });
            }
        }

        private static void Apply(Image image, byte[] data)
        {
            if (image == null) return;
            var texture = new Texture2D(2, 2, TextureFormat.RGBA32, false);
            if (!texture.LoadImage(data))
            {
                UnityEngine.Object.Destroy(texture);
                Debug.LogWarning($"[next-stickers] LoadImage failed: bytes={data?.Length ?? -1}");
                return;
            }
            if (!NextUi.SetOwnedSprite(image, texture))
            {
                UnityEngine.Object.Destroy(texture);
                Debug.LogWarning("[next-stickers] SetOwnedSprite failed");
                return;
            }
            image.color = Color.white;
        }

        /// <summary>氣泡裡的那張（不帶泡泡底：表情包本身就是一個圖案）。</summary>
        public static RectTransform Picture(string name, RectTransform parent, NextApi api, string url, float size = 112f)
        {
            var host = UiBuilder.NewRect(name, parent);
            UiBuilder.FixedWidth(host, size);
            UiBuilder.FixedHeight(host, size);
            var image = host.gameObject.AddComponent<Image>();
            image.preserveAspect = true;
            image.raycastTarget = false;
            image.color = new Color(1f, 1f, 1f, 0f);                 // 圖到之前透明（不要閃一塊白）
            Paint(api, image, url);
            return host;
        }

        // ── 表情盤 ──────────────────────────────────────────────────────────────

        /// <summary>打字列下面那一塊（跟「+」選單同一個位置；平常高度 0）。點一張 ⇒ <paramref name="pick"/>(id)。</summary>
        public void Build(RectTransform column, NextShell shell, Action<string> pick)
        {
            _shell = shell;
            _pick = pick;
            IsOpen = false;
            _loadedFor = "";
            _tray = UiBuilder.Column("ChatStickers", column, 4f, new RectOffset(8, 8, 10, 8), true, TextAnchor.UpperLeft);
            _trayHeight = UiBuilder.FixedHeight(_tray, 0f);
            _tray.gameObject.AddComponent<RectMask2D>();
            _trayGroup = _tray.gameObject.AddComponent<CanvasGroup>();
            _trayGroup.alpha = 0f;
            _trayGroup.interactable = false;
            _trayGroup.blocksRaycasts = false;
            var bg = UiBuilder.Backdrop("ChatStickersBg", _tray, NextTheme.Composer, radius: 14f);
            bg.gameObject.AddComponent<Shadow>().effectColor = NextTheme.Shadow;
            _hint = UiBuilder.Text("ChatStickersHint", _tray, "", NextTheme.NoteSize, NextTheme.InkSoft,
                                   TMPro.TextAlignmentOptions.MidlineLeft, noWrap: true);
            var content = NextUi.ScrollArea("ChatStickersScroll", _tray, 0f, null, out var scroll);
            _scroll = scroll;
            _content = content;
            var grid = new GameObject("ChatStickersGrid", typeof(RectTransform), typeof(GridLayoutGroup));
            _grid = (RectTransform)grid.transform;
            _grid.SetParent(content, false);
            var layout = grid.GetComponent<GridLayoutGroup>();
            layout.cellSize = new Vector2(Cell, Cell);
            layout.spacing = new Vector2(Spacing, Spacing);
            layout.constraint = GridLayoutGroup.Constraint.FixedColumnCount;
            layout.constraintCount = Columns;
            layout.childAlignment = TextAnchor.UpperCenter;
        }

        public void Toggle()
        {
            if (IsOpen) Close(); else Open();
        }

        public void Open()
        {
            if (_tray == null || IsOpen) return;
            IsOpen = true;
            Load();
            Animate(true, false);
            if (_shell != null) _shell.StartCoroutine(RetopAfterOpen());
        }

        public void Close(bool instant = false)
        {
            if (_tray == null || !IsOpen) return;
            IsOpen = false;
            Animate(false, instant);
        }

        private void Animate(bool open, bool instant)
        {
            _trayGroup.interactable = open;
            _trayGroup.blocksRaycasts = open;
            var from = _trayHeight.preferredHeight;
            var to = open ? TrayHeight : 0f;
            var column = _tray.parent as RectTransform;
            void Apply(float p)
            {
                var height = Mathf.Lerp(from, to, p);
                _trayHeight.minHeight = height;
                _trayHeight.preferredHeight = height;
                _trayGroup.alpha = open ? p : 1f - p;
                if (column != null) LayoutRebuilder.MarkLayoutForRebuild(column);
            }
            if (instant) { Apply(1f); return; }
            NextMotion.Play(_tray, 0.22f, Apply, 0f, NextMotion.OutCubic);
        }

        /// <summary>
        /// 第一次打開才抓清單（每個角色一包；換角色整頁重建，所以這裡只記一次）。
        /// 本機有上次那份 ⇒ 先鋪出來（離線也看得到），再帶著它的雜湊去問：304 就到此為止；
        /// 線上換過 ⇒ 存新的、整盤重鋪（每張再按自己的雜湊決定要不要重抓）。
        /// </summary>
        private void Load()
        {
            var entity = _shell.EntityId;
            if (_loading || _loadedFor == entity) return;
            var key = "stickers:" + entity;
            var cached = _shell.Api.CachedJson(key);
            var shown = "";
            if (cached != null)
            {
                Fill(cached);
                shown = cached.OptString("version");
            }
            else
            {
                NextUi.SetText(_hint, NextStrings.Get("loading"));
            }
            _loading = true;
            _shell.Api.GetStickers(entity, reply =>
            {
                _loading = false;
                if (_grid == null) return;                              // 回來前頁面已經換掉（U-7）
                if (reply.NotModified)
                {
                    _loadedFor = entity;
                    return;
                }
                if (!reply.Ok)
                {
                    // 有本機那份就留著它（下次打開再問）；沒有才講失敗
                    if (cached == null)
                        NextUi.SetText(_hint, NextStrings.F("read_fail", "reason", reply.UserMessage()));
                    return;
                }
                _loadedFor = entity;
                _shell.Api.StoreJson(key, reply, "version");
                if (cached != null && shown.Length > 0 && reply.Json.OptString("version") == shown) return;
                Fill(reply.Json);
            }, cached != null && _shell.Api.Cache != null ? _shell.Api.Cache.EtagOf(key) : "");
        }

        /// <summary>照清單鋪格子（先清掉舊的）。</summary>
        private void Fill(JObject data)
        {
            for (var i = _grid.childCount - 1; i >= 0; i--)
                UnityEngine.Object.Destroy(_grid.GetChild(i).gameObject);
            var rows = data.OptJArray("stickers");
            var count = rows != null ? rows.Length() : 0;
            NextUi.SetText(_hint, count > 0 ? NextStrings.Get("sticker_tray_hint") : NextStrings.Get("sticker_empty"));
            for (var i = 0; i < count; i++)
            {
                var row = rows.OptJObject(i);
                var id = row?.OptString("id");
                if (string.IsNullOrEmpty(id)) continue;
                AddCell(id, row.OptString("url"), row.OptString("hash"));
            }
            // ⚠️ **必須把高度釘死**（2026-10-29 真機實測：面板整片空白）。
            // 根因：**循環依賴**——捲動區的 content（`VerticalLayoutGroup` ＋ `ContentSizeFitter`）
            // 的高度來自 grid 的 preferredHeight；而 `GridLayoutGroup` 的 preferredHeight
            // 又要它自己先被佈局過（父級給了高度）才算得出來 ⇒ 兩者互相等，結果高度＝0，
            // 面板裡只露出第一行的前幾格（或整片空白，捲動之後尤其明顯）。
            // （貼圖全都下載成功——伺服器端 105 個請求全是 200——所以不是下載的問題。）
            //
            // 相片牆（`NextAlbumView`）沒這個毛病，因為它的 content 下面還有一顆分頁鈕撐著。
            PinGridHeight(count);
        }

        /// <summary>依格數算出網格高度並釘進 <see cref="LayoutElement"/>（不靠 UGUI 迭代求解）。</summary>
        private void PinGridHeight(int count)
        {
            if (_grid == null || count <= 0) return;
            var rows = Mathf.CeilToInt(count / (float)Columns);
            var height = rows * Cell + Mathf.Max(0, rows - 1) * Spacing;
            // ⚠️ **兩層都要釘**（真機實測：`grid=(422.00, 0.00) children=104`、`content=(422.00, 0.00)`）：
            // ① `grid`：它的父級（content）是一個 `VerticalLayoutGroup`，會用子物件的 preferredHeight 排版
            //    ⇒ 釘 grid 的高度，grid 才會被給到那個高度。
            // ② `content`：它自己被上層（托盤）的 `VerticalLayoutGroup` 排版 ⇒ 釘 content 的高度，
            //    捲動區才知道「內容有這麼高」才會給捲動範圍（否則捲動範圍＝0，一滑就一片空白）。
            //
            // ⚠️ **不能靠 `ContentSizeFitter`**：它量的是 grid 自己算出的 preferredHeight，
            // 而 `GridLayoutGroup` 的 preferredHeight 要它先被佈局過才算得出來 ⇒ 循環依賴 ⇒ 高度 0。
            // content 上的 `ContentSizeFitter`（`NextUi.ScrollArea` 加的）還在，但 `LayoutElement`
            // 的優先序比 `VerticalLayoutGroup` 高 ⇒ 它量到的就是這裡釘的高度。
            UiBuilder.FixedHeight(_grid, height);
            UiBuilder.FixedHeight(_content, height);
            // ⚠️ **寬度要釘成「剛好 5 列」**（真機實測：第 2 行整排右移一格、第 1 格空缺）。
            // 根因：`GridLayoutGroup` 用 grid 自己的 `rect.width` 算每列寬度
            // （(width − spacing×(n−1)) ÷ n），而 grid 沒有被任何東西撐寬
            // ⇒ 寬度不對 ⇒ 列寬不對 ⇒ 整個網格歪掉。
            //
            // ⇒ **不量 runtime 的寬度，直接用確定的值**：Columns × Cell ＋ (Columns−1) × Spacing。
            // （早先想用 `content.rect.width`，但釘高度的時候托盤還在動畫中 ⇒ 量到 0 ⇒ 釘不上去。）
            var width = Columns * Cell + (Columns - 1) * Spacing;
            var gridLe = _grid.GetComponent<LayoutElement>();
            if (gridLe == null) gridLe = _grid.gameObject.AddComponent<LayoutElement>();
            gridLe.minWidth = width;
            gridLe.preferredWidth = width;
            gridLe.flexibleWidth = 0f;
            LayoutRebuilder.MarkLayoutForRebuild(_grid.parent as RectTransform);
            LayoutRebuilder.MarkLayoutForRebuild(_content.parent as RectTransform);
            // 版面要重算才會生效（`LayoutElement` 只是宣告，真正排版在下一幀）
            Canvas.ForceUpdateCanvases();
            ScrollToTop();
        }

        /// <summary>捲回最上面。</summary>
        /// <remarks>
        /// ⚠️ 必須做：托盤是**動畫**開合的（高度 0 → 196），動畫期間 viewport 高度在變，
        /// `ScrollRect` 會把位置夾到當時的合法範圍裡（可能停在最下面）。
        /// 高度釘好之後不拉回頂端 ⇒ 面板開著但看到的是空白（真機實測 `vPos=1`、畫面全白）。
        /// </remarks>
        private void ScrollToTop()
        {
            if (_scroll == null) return;
            _scroll.verticalNormalizedPosition = 1f;
            _scroll.horizontalNormalizedPosition = 0f;
            if (_content != null) _content.anchoredPosition = new Vector2(0f, 0f);
        }

        /// <summary>開盤後等版面收斂再拉一次頂端（動畫 0.22 s ＋ 版面重排）。</summary>
        private System.Collections.IEnumerator RetopAfterOpen()
        {
            yield return null;
            yield return null;
            ScrollToTop();
            yield return null;
            yield return null;
        }

        private void AddCell(string id, string url, string hash)
        {
            var cell = new GameObject("Sticker_" + id, typeof(RectTransform), typeof(Image));
            var rect = (RectTransform)cell.transform;
            rect.SetParent(_grid, false);
            var hit = cell.GetComponent<Image>();
            hit.color = new Color(1f, 1f, 1f, 0.001f);                 // 透明但吃點擊
            var face = UiBuilder.Overlay("Face", rect);
            face.offsetMin = new Vector2(4f, 4f);
            face.offsetMax = new Vector2(-4f, -4f);
            var image = face.gameObject.AddComponent<Image>();
            image.preserveAspect = true;
            image.raycastTarget = false;
            image.color = new Color(1f, 1f, 1f, 0f);
            // 盤子裡用縮圖（一格 72 單位，用不到 256 px 的原圖；104 張第一次載入從約 9.5 MB 降下來）。
            // 縮圖的雜湊＝原圖雜湊＋寬（伺服器 `ThumbTag`），清單給的 hash 直接推得出來 ⇒ 照樣不必問。
            var full = string.IsNullOrEmpty(url) ? UrlOf(_shell.EntityId, id) : url;
            Paint(_shell.Api, image, full + "?w=" + TrayThumb,
                  string.IsNullOrEmpty(hash) ? "" : hash + "-w" + TrayThumb);
            var button = cell.AddComponent<Button>();
            button.targetGraphic = hit;
            button.transition = Selectable.Transition.None;
            button.onClick.AddListener(() => _pick?.Invoke(id));
            cell.AddComponent<NextPress>();
        }
    }
}
