using BlueAgent.Logic;
using BlueAgent.Screens;
using UnityEngine;
using UnityEngine.UI;

namespace BlueAgent.Next
{
    /// <summary>
    /// 立繪左上角的公開心情徽章。資料只來自 App Server 的 `/v1/moods` 與
    /// `mood.updated`；本地 SQLite 先提供離線快照，權威回包再對帳。
    /// Agent 的私有內心狀態不進這個模型，也不會在這裡觸發 Agent 回合。
    /// </summary>
    public sealed class NextMoodBadge : MonoBehaviour
    {
        private const float RefreshSeconds = 120f;
        private static int _eventVersion;

        /// <summary>設定頁用來擋住「較早 GET 回包蓋掉較晚事件」的代次。</summary>
        public static int Version { get { return _eventVersion; } }
        public static NextMoodBadge Current { get; private set; }

        private NextShell _shell;
        private string _entity;
        private Image _dotImage;
        private Component _label;
        private Component _note;
        private CanvasGroup _group;
        private float _nextAt;
        private bool _loading;

        public static void Attach(RectTransform holder, NextShell shell, string entity)
        {
            if (holder == null || shell == null || string.IsNullOrEmpty(entity)) return;
            var pill = UiBuilder.Row("MoodBadge", holder, 6f, new RectOffset(6, 10, 4, 4), TextAnchor.MiddleLeft, false);
            var element = pill.GetComponent<LayoutElement>() ?? pill.gameObject.AddComponent<LayoutElement>();
            element.ignoreLayout = true;
            pill.anchorMin = pill.anchorMax = pill.pivot = new Vector2(0f, 1f);
            pill.anchoredPosition = new Vector2(8f, -8f);
            var fitter = pill.gameObject.AddComponent<ContentSizeFitter>();
            fitter.horizontalFit = ContentSizeFitter.FitMode.PreferredSize;
            fitter.verticalFit = ContentSizeFitter.FitMode.PreferredSize;
            UiBuilder.Backdrop("MoodBadgeBg", pill, new Color(1f, 1f, 1f, 0.82f), radius: 10f);

            var badge = pill.gameObject.AddComponent<NextMoodBadge>();
            badge._shell = shell;
            badge._entity = entity;
            badge._group = pill.gameObject.AddComponent<CanvasGroup>();
            badge._group.alpha = 0f;
            badge._group.interactable = false;
            badge._group.blocksRaycasts = false;

            var dot = new GameObject("MoodDot", typeof(RectTransform)).GetComponent<RectTransform>();
            dot.SetParent(pill, false);
            var dotLayout = dot.gameObject.AddComponent<LayoutElement>();
            dotLayout.preferredWidth = dotLayout.minWidth = 10f;
            dotLayout.preferredHeight = dotLayout.minHeight = 10f;
            badge._dotImage = dot.gameObject.AddComponent<Image>();
            badge._dotImage.raycastTarget = false;
            UiBuilder.RoundCorners(badge._dotImage, 5f, 5f, 5f, 5f);

            var texts = UiBuilder.Column("MoodBadgeTexts", pill, 0f, null, false, TextAnchor.MiddleLeft);
            badge._label = UiBuilder.Text("MoodBadgeLabel", texts, "…", NextTheme.LabelSize, NextTheme.Ink,
                                          TMPro.TextAlignmentOptions.MidlineLeft, noWrap: true, bold: true);
            badge._note = UiBuilder.Text("MoodBadgeNote", texts, "", NextTheme.TinySize,
                                         NextTheme.InkSoft, TMPro.TextAlignmentOptions.MidlineLeft, noWrap: true);
            badge._note.gameObject.SetActive(false);
            badge.PaintCached();
            Current = badge;
        }

        public static void Sync()
        {
            if (Current != null) Current._nextAt = 0f;
        }

        /// <summary>殼已先把事件寫入 SQLite；這裡只更新目前舞台上的徽章。</summary>
        public static void OnEvent(JObject frame)
        {
            var mood = frame?.OptJObject("payload")?.OptJObject("mood");
            if (mood == null || string.IsNullOrEmpty(mood.OptString("account_id"))) return;
            _eventVersion++;
            var badge = Current;
            if (badge == null || mood.OptString("account_id") != badge._entity) return;
            badge._nextAt = Time.unscaledTime + RefreshSeconds;
            badge.Show(mood);
        }

        private void PaintCached()
        {
            var mood = _shell.Api.Social?.Mood(_entity);
            if (mood == null) SetVisible(false);
            else Show(mood);
        }

        private void SetVisible(bool visible)
        {
            if (_group != null) _group.alpha = visible ? 1f : 0f;
        }

        private void Update()
        {
            if (_loading || Time.unscaledTime < _nextAt) return;
            _loading = true;
            _nextAt = Time.unscaledTime + RefreshSeconds;
            var version = _eventVersion;
            var repository = _shell.Api.Social;
            _shell.Api.Get("/v1/moods", reply =>
            {
                _loading = false;
                if (this == null || !reply.Ok) return; // 離線時保留本地快照。
                if (version != _eventVersion || repository == null || !ReferenceEquals(repository, _shell.Api.Social))
                {
                    PaintCached();                     // 事件較新：不能用整包快照回蓋。
                    return;
                }
                repository.ReplaceMoods(reply.Json.OptJArray("moods"));
                PaintCached();
            });
        }

        private void Show(JObject mood)
        {
            if (mood == null || mood.OptString("account_id") != _entity) return;
            var key = mood.OptString("key");
            if (!IsPublicKey(key)) { SetVisible(false); return; }
            var color = ColorOf(key);
            NextUi.SetText(_label, NextStrings.Has("mood_" + key) ? NextStrings.Get("mood_" + key) : key);
            NextUi.SetColor(_label, color);
            _dotImage.color = color;
            var note = mood.OptString("note").Trim();
            NextUi.SetText(_note, note);
            _note.gameObject.SetActive(note.Length > 0);
            SetVisible(true);
        }

        private static bool IsPublicKey(string key)
        {
            return key == "calm" || key == "happy" || key == "excited" ||
                   key == "irritated" || key == "down" || key == "lonely";
        }

        private static Color ColorOf(string key)
        {
            switch (key)
            {
                case "excited": return NextTheme.Pink;
                case "happy": return NextTheme.Ok;
                case "irritated": return NextTheme.Danger;
                case "down": return NextTheme.SkyDeep;
                case "lonely": return NextTheme.Pink;
                default: return NextTheme.InkSoft;
            }
        }

        private void OnDestroy()
        {
            if (Current == this) Current = null;
        }
    }
}
