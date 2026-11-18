using BlueAgent.Logic;
using BlueAgent.Screens;
using UnityEngine;
using UnityEngine.UI;

namespace BlueAgent.Next
{
    /// <summary>
    /// 送過的照片（P4）：`GET /v1/media` 的縮圖牆，點一張看全圖。從設定頁進來。
    ///
    /// 只讀：圖片是**伺服器上的那份**（她產的圖＋送過的圖），縮圖用 `/v1/media/&lt;id&gt;` 直接抓。
    /// 「從手機相冊挑一張送出去」是聊天頁「+」的「写真」（`NextShell.PickImage`），不在這一頁。
    /// </summary>
    public static class NextAlbumView
    {
        private const int PageSize = 30;

        public static void Build(RectTransform body, NextShell shell)
        {
            var column = UiBuilder.Column("AlbumBody", body, 10f, new RectOffset(12, 12, 10, 12), true, TextAnchor.UpperLeft);
            UiBuilder.Weight(column, 1f);

            // 相簿只是相簿（照片牆，從設定頁進來）：送圖只走聊天頁「+」的「写真」那一個入口（2026-10-29 owner）
            NextUi.Header("AlbumHead", column, NextStrings.Get("album_title"), shell.ShowSettings);

            var hintRow = UiBuilder.Row("AlbumHintRow", column, 6f, new RectOffset(2, 2, 0, 0), TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(hintRow, 20f);
            NextUi.Accent("AlbumAccent", hintRow, 4f, 14f);
            var hint = UiBuilder.Text("AlbumHint", hintRow, NextStrings.Get("loading"), NextTheme.NoteSize, NextTheme.InkSoft,
                                      TMPro.TextAlignmentOptions.MidlineLeft, weight: 1f, noWrap: true);

            // 30 張就是十行，不能直接把整面牆塞進固定高度的頁面；標題與計數固定，照片和分頁鈕一起捲。
            var scrollContent = NextUi.ScrollArea("AlbumScroll", column, 10f, null, out _);
            var grid = new GameObject("AlbumGrid", typeof(RectTransform), typeof(GridLayoutGroup));
            var gridRect = (RectTransform)grid.transform;
            gridRect.SetParent(scrollContent, false);
            var layout = grid.GetComponent<GridLayoutGroup>();
            layout.cellSize = new Vector2(140f, 140f);
            layout.spacing = new Vector2(8f, 8f);
            layout.constraint = GridLayoutGroup.Constraint.FixedColumnCount;
            layout.constraintCount = 3;
            layout.childAlignment = TextAnchor.UpperLeft;

            var more = NextUi.Button("AlbumMore", scrollContent, NextStrings.Get("album_more"), NextUi.Style.Secondary, 34f, 0f,
                                     "chevron-down");
            more.gameObject.SetActive(false);

            var offset = 0;
            var loading = false;

            void LoadPage()
            {
                if (loading) return;
                loading = true;
                shell.Api.GetMedia(offset, PageSize, reply =>
                {
                    loading = false;
                    // 列表回來前已經離開相簿（`ClearBody` 把整頁銷毀了）⇒ 什麼都不寫（U-7）
                    if (gridRect == null) return;
                    if (!reply.Ok)
                    {
                        NextUi.SetText(hint, NextStrings.F("read_fail", "reason", reply.UserMessage()));
                        return;
                    }
                    var items = reply.Json.OptJArray("items");
                    var total = reply.Json.OptInt("total");
                    var count = items != null ? items.Length() : 0;
                    offset += count;
                    NextUi.SetText(hint, NextStrings.F("album_count", "total", total, "shown", offset));
                    for (var i = 0; items != null && i < count; i++)
                    {
                        var row = items.OptJObject(i);
                        var id = row?.OptString("id");
                        if (string.IsNullOrEmpty(id)) continue;
                        AddThumb(gridRect, shell, id);
                    }
                    more.gameObject.SetActive(offset < total);
                    UiBuilder.Settle((RectTransform)body);
                });
            }

            more.onClick.AddListener(LoadPage);
            LoadPage();
        }

        private static void AddThumb(RectTransform grid, NextShell shell, string mediaId)
        {
            // 白框（洗出來的照片那種）＋落影；照片本身內縮 4 單位
            var cell = new GameObject("Thumb_" + mediaId, typeof(RectTransform), typeof(Image));
            var rect = (RectTransform)cell.transform;
            rect.SetParent(grid, false);
            var frame = cell.GetComponent<Image>();
            frame.color = Color.white;
            frame.raycastTarget = true;
            var drop = cell.AddComponent<Shadow>();
            drop.effectColor = NextTheme.Shadow;
            drop.effectDistance = new Vector2(0f, -4f);

            var photoRect = UiBuilder.Overlay("Photo", rect);
            photoRect.offsetMin = new Vector2(4f, 4f);
            photoRect.offsetMax = new Vector2(-4f, -4f);
            var image = photoRect.gameObject.AddComponent<Image>();
            image.color = NextTheme.SkyPale;
            image.raycastTarget = false;

            // 牆上只要縮圖（以前抓原圖：一頁 30 張可能十幾 MB）；點開全螢幕才抓原圖
            shell.Api.GetMediaTexture(mediaId, texture =>
            {
                // 抓不到就留底色；回來前相簿已經關掉（image 已銷毀）⇒ 貼圖就地釋放、不寫（U-7）
                if (NextUi.SetOwnedSprite(image, texture)) image.color = Color.white;
            }, NextApi.ThumbWidth);

            var button = cell.AddComponent<Button>();
            button.targetGraphic = frame;
            button.transition = Selectable.Transition.None;
            button.onClick.AddListener(() => shell.ShowMediaFull(mediaId));
            cell.AddComponent<NextPress>();
        }
    }
}
