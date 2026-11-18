#if BA_SELFTEST
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.IO;
using System.Text;
using BlueAgent.Screens;
using TMPro;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.UI;

namespace BlueAgent.Next
{
    /// <summary>
    /// 驗收鉤子（只在 `BA_SELFTEST=1` 的包裡）：把每一頁、每一種界面語言畫面上的字倒成文字檔。
    ///
    /// 為什麼要有：i18n 的閘門只看得到原始碼，看不到「伺服器回來的字＋我們的字」拼起來的實際畫面；
    /// 截圖又要桌面權限。這支在無頭的播放器裡逐頁切過去，把可見的 TMP 文字（含輸入框的提示字）寫成
    /// `{BA_NEXT_UI_DUMP}/{locale}/{page}.txt`（每行：物件名、字型家族、文字），拿去掃「中文頁冒出假名」、
    /// 「中文頁用了日語字型」之類的混用。
    /// 用法：`BA_NEXT_UI_DUMP=/路徑 <播放器>`（本機要先存好伺服器位址與 token）；跑完自己結束。
    /// 加 `BA_NEXT_UI_SHOTS=1` 每頁另存一張 PNG（設計驗收用，不必桌面截圖權限）。
    /// </summary>
    public sealed class NextUiDump : MonoBehaviour
    {
        private const int ShotWidth = 720;
        private const int ShotHeight = 1280;
        private string _folder;

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        private static void Install()
        {
            var folder = System.Environment.GetEnvironmentVariable("BA_NEXT_UI_DUMP");
            if (string.IsNullOrEmpty(folder)) return;
            // 無頭播放器的螢幕固定 640×480（不吃 -screen-width）⇒ 相機先釘成手機的 9:16，
            // 殼在 Start 建畫布時就照直式排（AfterSceneLoad 在 Awake 之後、Start 之前）
            foreach (var camera in Object.FindObjectsByType<Camera>(FindObjectsSortMode.None))
                camera.aspect = ShotWidth / (float)ShotHeight;
            NextCallView.NoMic = true;            // 拍通話頁不開這台機器的麥克風（也不跳權限框）
            var go = new GameObject("NextUiDump");
            DontDestroyOnLoad(go);
            go.AddComponent<NextUiDump>()._folder = folder;
        }

        private IEnumerator Start()
        {
            NextShell shell = null;
            for (var i = 0; i < 300 && (shell == null || shell.Me == null); i++)
            {
                shell = FindFirstObjectByType<NextShell>();
                yield return new WaitForSeconds(0.1f);
            }
            if (shell == null || shell.Me == null)
            {
                Debug.LogError("[next-dump] 沒登入成功（先存好伺服器位址與 token）");
                Application.Quit(2);
                yield break;
            }
            var m0Folder = System.Environment.GetEnvironmentVariable("BA_NEXT_M0_PROBE");
            if (!string.IsNullOrEmpty(m0Folder))
            {
                yield return M0Probe(shell, m0Folder,
                    System.Environment.GetEnvironmentVariable("BA_NEXT_M0_PHASE") ?? "inject");
                yield break;
            }
            var m1Folder = System.Environment.GetEnvironmentVariable("BA_NEXT_M1_PROBE");
            if (!string.IsNullOrEmpty(m1Folder))
            {
                yield return M1Probe(shell, m1Folder);
                yield break;
            }
            var original = NextConfig.UiLanguage;
            foreach (var locale in BlueAgent.Logic.NextText.Locales)
            {
                shell.ApplyLanguage(locale);
                yield return Page(shell, locale, "chat", shell.CloseSettings);
                yield return Page(shell, locale, "chat-sheet", () => Tap("ChatAlbum"));     // 「+」彈起底部選單
                yield return Page(shell, locale, "chat-voice", () => { Tap("ChatAlbum"); Tap("ChatMic"); });  // 語音模式
                yield return Page(shell, locale, "chat-voice-send", () => Tap("ChatSend"));    // 沒錄就按送出 ⇒ 提示先按住
                yield return Page(shell, locale, "chat-keyboard", () => Tap("ChatKeyboard"));  // 回打字
                // 長文：欄位長高、兩側鈕貼底（不用假名：中文頁會被當成冒出假名）
                yield return Page(shell, locale, "chat-multiline", () => SetDraft(
                    "Line 1\nLine 2 is long enough to wrap inside the field, long enough to wrap\nLine 3"));
                yield return Page(shell, locale, "chat-stickers", () => { SetDraft(""); Tap("ChatSticker"); });   // 笑臉打開表情盤
                if (shell.IsMaster)                                                           // 單人通話（打了就掛）
                    yield return Page(shell, locale, "call", shell.ShowCall);
                foreach (var page in new[] { "audio", "bg", "service", "diag" })
                {
                    var target = page;
                    yield return Page(shell, locale, "settings-" + page, () => shell.ShowSettingsPage(target));
                }
                yield return Page(shell, locale, "status", () => shell.ShowSubPage(NextStrings.Get("status_page"),
                    pane => NextStatusView.Build(pane, shell)));
                if (shell.IsMaster)
                    yield return Page(shell, locale, "accounts", () => shell.ShowSubPage(NextStrings.Get("accounts"),
                        pane => NextAccountsView.Build(pane, shell)));
                yield return Page(shell, locale, "album", shell.ShowAlbum);
            }
            // 游標（2026-10-30 探針定案）：聚焦聊天欄、打字，隔 0.3 秒拍 4 張＋倒出 caret 的內部狀態
            shell.ApplyLanguage(BlueAgent.Logic.NextText.Ja);
            shell.CloseSettings();
            yield return new WaitForSeconds(1.0f);
            yield return CaretProbe(Path.Combine(_folder, "caret"), "ChatInput");

            // 真的點一下「ノリ」那顆切換鈕（走 EventSystem 的射線＋點擊，不是直接叫 onClick），看有沒有換過去
            shell.ApplyLanguage(BlueAgent.Logic.NextText.Ja);
            shell.CloseSettings();
            yield return new WaitForSeconds(1.0f);
            var switched = Tap("Entity_nori");
            yield return new WaitForSeconds(1.5f);
            Directory.CreateDirectory(Path.Combine(_folder, "switch"));
            File.WriteAllText(Path.Combine(_folder, "switch", "result.txt"),
                              "tapped=" + switched + "\nentity=" + shell.EntityId + "\ntitle=" + shell.EntityDisplayName + "\n");
            Tap("Entity_mira");
            yield return new WaitForSeconds(1.0f);

            // 按住說話（只在放了 inject.wav 時跑：自測包的錄音走檔案，不碰這台的麥克風）
            if (File.Exists(Path.Combine(Application.persistentDataPath, "inject.wav")))
            {
                var hold = Path.Combine(_folder, "hold");
                Directory.CreateDirectory(hold);
                shell.CloseSettings();
                yield return new WaitForSeconds(1.0f);
                Tap("ChatMic");
                yield return new WaitForSeconds(0.5f);
                var pressed = Pointer("ChatHold", ExecuteEvents.pointerDownHandler);
                yield return new WaitForSeconds(1.0f);
                Shoot(Path.Combine(hold, "1-holding.png"));
                var holdingText = TextOf("ChatHoldText");
                Pointer("ChatHold", ExecuteEvents.pointerUpHandler);
                yield return new WaitForSeconds(0.5f);
                Shoot(Path.Combine(hold, "2-released.png"));
                var releasedText = TextOf("ChatHoldText");
                Tap("ChatSend");
                yield return new WaitForSeconds(2.0f);
                Shoot(Path.Combine(hold, "3-sent.png"));
                File.WriteAllText(Path.Combine(hold, "result.txt"),
                    "pressed=" + pressed + "\nholding=" + holdingText + "\nreleased=" + releasedText
                    + "\nafter_send=" + TextOf("ChatHoldText") + "\nhint=" + TextOf("ChatHint")
                    + "\nvoice_bubble=" + (GameObject.FindObjectsByType<TMP_Text>(FindObjectsSortMode.None)
                        .Any(text => text.name == "BubbleTagText" && text.text.StartsWith("♪"))) + "\n");
                Tap("ChatKeyboard");
                yield return new WaitForSeconds(0.5f);
            }

            // 動態：從聊天進設定那一下（掃光＋淡入），每 0.06 秒拍一張
            shell.ApplyLanguage(BlueAgent.Logic.NextText.Ja);
            shell.CloseSettings();
            yield return new WaitForSeconds(1.0f);
            if (System.Environment.GetEnvironmentVariable("BA_NEXT_UI_SHOTS") == "1")
            {
                var motion = Path.Combine(_folder, "motion");
                Directory.CreateDirectory(motion);
                shell.ShowSettings();
                for (var frame = 0; frame < 6; frame++)
                {
                    yield return new WaitForSeconds(0.06f);
                    yield return new WaitForEndOfFrame();
                    Shoot(Path.Combine(motion, "settings-" + frame + ".png"));
                }
            }
            // 登入頁最後拍：登出會清掉本機 token（下一輪要重新存）
            foreach (var locale in BlueAgent.Logic.NextText.Locales)
            {
                shell.ApplyLanguage(locale);
                yield return Page(shell, locale, "login", shell.Logout);
            }
            yield return CaretProbe(Path.Combine(_folder, "caret-login"), "LoginToken");   // 單行欄（登入 token）也要有游標
            shell.ApplyLanguage(original);
            Debug.Log("[next-dump] 完成 ⇒ " + _folder);
            Application.Quit(0);
        }

        /// <summary>
        /// 點得到嗎：對畫面上每一顆可用的按鈕，在它正中央用真的 `EventSystem` 打一次射線，
        /// 最上面那個物件交給 `IPointerClickHandler` 冒泡，接到的不是這顆按鈕 ⇒ 點了沒反應。
        /// 截圖看不出這種錯（2026-10-29：角色切換鈕的底板不吃點擊，整顆失靈）。
        /// 被捲動區裁掉（中心不在遮罩裡）的按鈕不算。
        /// </summary>
        private static List<string> Unclickable()
        {
            var dead = new List<string>();
            var events = EventSystem.current;
            if (events == null) { dead.Add("<no EventSystem>"); return dead; }
            var hits = new List<RaycastResult>();
            foreach (var button in FindObjectsByType<Button>(FindObjectsSortMode.None))
            {
                if (!button.isActiveAndEnabled || !button.interactable) continue;
                var rect = (RectTransform)button.transform;
                var canvas = button.GetComponentInParent<Canvas>();
                var camera = canvas != null ? canvas.worldCamera : null;
                var screen = RectTransformUtility.WorldToScreenPoint(camera, rect.TransformPoint(rect.rect.center));
                var clipped = false;
                foreach (var mask in rect.GetComponentsInParent<RectMask2D>())
                    if (!RectTransformUtility.RectangleContainsScreenPoint(mask.rectTransform, screen, camera)) clipped = true;
                if (clipped) continue;
                hits.Clear();
                events.RaycastAll(new PointerEventData(events) { position = screen }, hits);
                var top = hits.Count > 0 ? ExecuteEvents.GetEventHandler<IPointerClickHandler>(hits[0].gameObject) : null;
                if (top != button.gameObject)
                    dead.Add(button.name + " (hit " + (hits.Count > 0 ? hits[0].gameObject.name : "nothing") + ")");
            }
            return dead;
        }

        /// <summary>在某顆按鈕正中央送一次真的點擊（射線 → 最上層 → 冒泡到 IPointerClickHandler）。回 false＝沒點到它。</summary>
        /// <summary>聊天輸入欄直接填字（跟打字一樣觸發 onValueChanged ⇒ 欄位照常長高）。</summary>
        private static void SetDraft(string text)
        {
            var field = GameObject.Find("ChatInput")?.GetComponent<TMP_InputField>();
            if (field != null) field.text = text;
        }

        /// <summary>
        /// 游標驗收（2026-10-30 探針定案）：聚焦 <paramref name="name"/> 那一格、打 abc，
        /// 隔 0.3 秒拍 4 張＋倒出 caret 的內部狀態（`probe.txt`：網格頂點數該在 4／0 之間交替）。
        /// </summary>
        private static IEnumerator CaretProbe(string folder, string name)
        {
            Directory.CreateDirectory(folder);
            var field = GameObject.Find(name)?.GetComponent<TMP_InputField>();
            var log = new StringBuilder();
            if (field == null)
            {
                File.WriteAllText(Path.Combine(folder, "probe.txt"), "no " + name + "\n");
                yield break;
            }
            EventSystem.current?.SetSelectedGameObject(field.gameObject);
            field.ActivateInputField();
            yield return new WaitForSeconds(0.5f);
            field.text = "abc";
            field.MoveTextEnd(false);
            yield return new WaitForSeconds(0.5f);
            for (var i = 0; i < 4; i++)
            {
                Shoot(Path.Combine(folder, "shot" + i + ".png"));
                log.Append(CaretState(field)).Append('\n');
                yield return new WaitForSeconds(0.3f);
            }
            File.WriteAllText(Path.Combine(folder, "probe.txt"), log.ToString());
            field.text = "";
            field.DeactivateInputField();
            EventSystem.current?.SetSelectedGameObject(null);
        }

        private static object Private(object target, string name)
        {
            const System.Reflection.BindingFlags flags = System.Reflection.BindingFlags.Instance
                | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Public;
            for (var type = target.GetType(); type != null; type = type.BaseType)
            {
                var info = type.GetField(name, flags);
                if (info != null) return info.GetValue(target);
                var prop = type.GetProperty(name, flags);
                if (prop != null) return prop.GetValue(target);
                var method = type.GetMethod(name, flags, null, System.Type.EmptyTypes, null);
                if (method != null) return method.Invoke(target, null);
            }
            return "?";
        }

        private static string CaretState(TMP_InputField field)
        {
            var renderer = Private(field, "m_CachedInputRenderer") as CanvasRenderer;
            var caret = Private(field, "caretRectTrans") as RectTransform;
            var mesh = Private(field, "mesh") as Mesh;
            var text = new StringBuilder();
            text.Append("focused=").Append(field.isFocused)
                .Append(" inPlace=").Append(Private(field, "InPlaceEditing"))
                .Append(" visible=").Append(Private(field, "m_CaretVisible"))
                .Append(" blink=").Append(Private(field, "m_BlinkCoroutine") != null)
                .Append(" caretPos=").Append(field.caretPosition)
                .Append(" chars=").Append(field.textComponent.textInfo.characterCount)
                .Append(" width=").Append(field.caretWidth)
                .Append(" color=").Append(field.caretColor)
                .Append(" soft=").Append(Private(field, "m_SoftKeyboard") != null);
            if (mesh != null) text.Append(" meshVerts=").Append(mesh.vertexCount).Append(" bounds=").Append(mesh.bounds);
            if (renderer != null)
            {
                text.Append(" cull=").Append(renderer.cull)
                    .Append(" clip=").Append(renderer.hasRectClipping)
                    .Append(" alpha=").Append(renderer.GetAlpha())
                    .Append(" inheritedAlpha=").Append(renderer.GetInheritedAlpha())
                    .Append(" mats=").Append(renderer.materialCount)
                    .Append(" mat=").Append(renderer.materialCount > 0 && renderer.GetMaterial(0) != null
                                            ? renderer.GetMaterial(0).shader.name : "null")
                    .Append(" depth=").Append(renderer.absoluteDepth);
                var go = renderer.gameObject;
                text.Append(" active=").Append(go.activeInHierarchy)
                    .Append(" layer=").Append(go.layer)
                    .Append(" sibling=").Append(go.transform.GetSiblingIndex())
                    .Append(" graphicColor=").Append(go.GetComponent<Graphic>()?.color);
            }
            if (caret != null)
            {
                var corners = new Vector3[4];
                caret.GetWorldCorners(corners);
                text.Append(" caretRect=").Append(caret.rect).Append(" caretWorld=").Append(corners[0]).Append("..").Append(corners[2]);
                var viewport = field.textViewport;
                viewport.GetWorldCorners(corners);
                text.Append(" viewportWorld=").Append(corners[0]).Append("..").Append(corners[2]);
                text.Append(" textLocal=").Append(field.textComponent.rectTransform.localPosition)
                    .Append(" caretLocal=").Append(caret.localPosition);
            }
            return text.ToString();
        }

        private static bool Tap(string name)
        {
            var target = GameObject.Find(name);
            var events = EventSystem.current;
            if (target == null || events == null) return false;
            var rect = (RectTransform)target.transform;
            var canvas = target.GetComponentInParent<Canvas>();
            var camera = canvas != null ? canvas.worldCamera : null;
            var screen = RectTransformUtility.WorldToScreenPoint(camera, rect.TransformPoint(rect.rect.center));
            var data = new PointerEventData(events) { position = screen };
            var hits = new List<RaycastResult>();
            events.RaycastAll(data, hits);
            if (hits.Count == 0) return false;
            var handler = ExecuteEvents.GetEventHandler<IPointerClickHandler>(hits[0].gameObject);
            if (handler != target) return false;
            data.pointerPress = handler;
            ExecuteEvents.Execute(handler, data, ExecuteEvents.pointerClickHandler);
            return true;
        }

        /// <summary>對某顆送一個指標事件（按下／放開），走跟 Tap 一樣的射線命中檢查。</summary>
        private static bool Pointer<T>(string name, ExecuteEvents.EventFunction<T> what) where T : IEventSystemHandler
        {
            var target = GameObject.Find(name);
            var events = EventSystem.current;
            if (target == null || events == null) return false;
            var rect = (RectTransform)target.transform;
            var canvas = target.GetComponentInParent<Canvas>();
            var camera = canvas != null ? canvas.worldCamera : null;
            var data = new PointerEventData(events)
            {
                position = RectTransformUtility.WorldToScreenPoint(camera, rect.TransformPoint(rect.rect.center)),
            };
            var hits = new List<RaycastResult>();
            events.RaycastAll(data, hits);
            if (hits.Count == 0) return false;
            var handler = ExecuteEvents.GetEventHandler<T>(hits[0].gameObject);
            if (handler != target) return false;
            ExecuteEvents.Execute(handler, data, what);
            return true;
        }

        private static string TextOf(string name)
        {
            var go = GameObject.Find(name);
            var text = go != null ? go.GetComponent<TMP_Text>() : null;
            return text != null ? text.text : "<none>";
        }

        // M0 的真播放器驗收：從公開 delivery 入口注入，繞過桌面的像素觸控，但不繞過
        // SetChatVisible → ConversationStore → PlayerPrefs → 重啟後 ShowStashed 這條正式路徑。
        private const string M0First = "M0-CURRENT-A-20261104";
        private const string M0Second = "M0-CURRENT-B-20261104";
        private const string M0Other = "M0-OTHER-20261104";

        private IEnumerator M0Probe(NextShell shell, string folder, string phase)
        {
            Directory.CreateDirectory(folder);
            var current = shell.EntityId;
            var other = current == "aster" ? "nori" : "aster";
            var chat = shell.SelfTestChat;
            if (chat == null)
            {
                File.WriteAllText(Path.Combine(folder, phase + ".txt"), "pass=False\nreason=no chat\n");
                Application.Quit(4);
                yield break;
            }

            if (phase == "inject")
            {
                shell.ShowSettingsPage("service");
                yield return new WaitForSeconds(1.0f);
                chat.OnDelivery(new BlueAgent.Logic.JObject(
                    "{\"kind\":\"delivery\",\"turn_id\":\"m0-current-a-20261104\",\"text\":\"" + M0First + "\"}"));
                chat.OnDelivery(new BlueAgent.Logic.JObject(
                    "{\"kind\":\"delivery\",\"turn_id\":\"m0-current-b-20261104\",\"text\":\"" + M0Second + "\"}"));
                // 同 id 重放不能多一條；沒有 entity 的舊幀應歸到目前角色。
                chat.OnDelivery(new BlueAgent.Logic.JObject(
                    "{\"kind\":\"delivery\",\"turn_id\":\"m0-current-a-20261104\",\"text\":\"" + M0First + "\"}"));
                chat.OnDelivery(new BlueAgent.Logic.JObject(
                    "{\"kind\":\"delivery\",\"entity\":\"" + other +
                    "\",\"turn_id\":\"m0-other-20261104\",\"text\":\"" + M0Other + "\"}"));
                yield return new WaitForSeconds(0.5f);
                var pending = NextConfig.LoadConversationStore().Pending;
                var currentPending = pending.Count(item => item.Entity == current);
                var otherPending = pending.Count(item => item.Entity == other);
                var hidden = M0Count(M0First) == 0 && M0Count(M0Second) == 0 && M0Count(M0Other) == 0;
                var pass = hidden && currentPending == 2 && otherPending == 1;
                File.WriteAllText(Path.Combine(folder, "inject.txt"),
                    "pass=" + pass + "\noffscreen_hidden=" + hidden +
                    "\ncurrent=" + current + "\ncurrent_pending=" + currentPending +
                    "\nother=" + other + "\nother_pending=" + otherPending + "\n");
                Shoot(Path.Combine(folder, "inject-settings.png"));
                Debug.Log("[next-m0] inject pass=" + pass + " ⇒ " + folder);
                Application.Quit(pass ? 0 : 5);
                yield break;
            }

            if (phase == "verify")
            {
                shell.CloseSettings();
                yield return new WaitForSeconds(3.0f);
                var pending = NextConfig.LoadConversationStore().Pending;
                var currentPending = pending.Count(item => item.Entity == current);
                var otherPending = pending.Count(item => item.Entity == other);
                var firstCount = M0Count(M0First);
                var secondCount = M0Count(M0Second);
                var ordered = M0Y(M0First) > M0Y(M0Second);
                // 1:1 頁不再有角色藥丸（owner 2026-11-07）：未讀看會話列表用的同一個計數。
                var pillUnread = shell.Chat != null && shell.Chat.UnreadFor(other) == 1;
                var restored = firstCount == 1 && secondCount == 1 && M0Count(M0Other) == 0 &&
                               currentPending == 0 && otherPending == 1 && ordered && pillUnread;
                Shoot(Path.Combine(folder, "verify-current.png"));

                // 換人走會話列表點行的同一條路（SwitchEntity），不再點藥丸。
                shell.SwitchEntity(other, shell.EntityNameOf(other));
                var tapped = true;
                yield return new WaitForSeconds(4.0f);
                pending = NextConfig.LoadConversationStore().Pending;
                var switched = tapped && shell.EntityId == other;
                var otherRestored = M0Count(M0Other) == 1 &&
                                    pending.Count(item => item.Entity == other) == 0;
                var pass = restored && switched && otherRestored;
                File.WriteAllText(Path.Combine(folder, "verify.txt"),
                    "pass=" + pass + "\nrestored_current=" + restored +
                    "\nfirst_count=" + firstCount + "\nsecond_count=" + secondCount +
                    "\nordered=" + ordered + "\npill_unread=" + pillUnread +
                    "\ncurrent_pending=" + currentPending + "\nother_pending_before=" + otherPending +
                    "\nswitched=" + switched + "\nother_restored=" + otherRestored + "\n");
                Shoot(Path.Combine(folder, "verify-other.png"));
                Debug.Log("[next-m0] verify pass=" + pass + " ⇒ " + folder);
                Application.Quit(pass ? 0 : 6);
                yield break;
            }

            if (phase == "keyboard")
            {
                shell.CloseSettings();
                yield return new WaitForSeconds(1.0f);
                var field = GameObject.Find("ChatInput")?.GetComponent<TMP_InputField>();
                if (field != null)
                {
                    EventSystem.current?.SetSelectedGameObject(field.gameObject);
                    field.ActivateInputField();
                }
                yield return new WaitForSeconds(1.5f);
                var focused = field != null && field.isFocused;
                var visible = TouchScreenKeyboard.visible;
                File.WriteAllText(Path.Combine(folder, "keyboard.txt"),
                    "field=" + (field != null) + "\nfocused=" + focused +
                    "\nvisible=" + visible + "\narea=" + TouchScreenKeyboard.area + "\n");
                Debug.Log("[next-m0] keyboard focused=" + focused + " visible=" + visible);
                // ネイティブ側の全画面撮影までキーボードを保持する。
                yield return new WaitForSeconds(45.0f);
                Application.Quit(focused && visible ? 0 : 8);
                yield break;
            }

            File.WriteAllText(Path.Combine(folder, "error.txt"), "unknown phase=" + phase + "\n");
            Application.Quit(7);
        }

        // M1 列表驗收：回包只進正式 Render，點列／返回走正式 EventSystem 射線。
        private static BlueAgent.Logic.JObject SampleSessions()
        {
            var now = System.DateTimeOffset.Now.ToUnixTimeSeconds();
            return new BlueAgent.Logic.JObject(
                "{\"agents\":[" +
                "{\"entity\":\"mira\",\"name\":\"Mira\",\"last\":\"M1-MIRA-LAST\",\"at\":" + now +
                ",\"turn_id\":\"m1-mira\",\"unread\":0}," +
                "{\"entity\":\"aster\",\"name\":\"Aster\",\"last\":\"M1-ASTER-LAST\",\"at\":" + (now - 120) +
                ",\"turn_id\":\"m1-aster\",\"unread\":0}," +
                "{\"entity\":\"nori\",\"name\":\"Nori\",\"last\":\"M1-NORI-LAST\",\"at\":" + (now - 86400) +
                ",\"turn_id\":\"m1-nori\",\"unread\":0}" +
                "],\"guests\":[],\"rooms\":[]}");
        }

        private IEnumerator M1Probe(NextShell shell, string folder)
        {
            Directory.CreateDirectory(folder);
            shell.SelfTestShowSessions(SampleSessions());
            shell.SelfTestChat.OnDelivery(new BlueAgent.Logic.JObject(
                "{\"kind\":\"delivery\",\"entity\":\"aster\",\"turn_id\":\"m1-list-unread\"," +
                "\"text\":\"M1-UNREAD-ASTER\"}"));
            // 在列表上重新消費同一份回包，讓本地 ConversationStore 的未讀覆蓋服務端 0。
            shell.SelfTestShowSessions(SampleSessions());
            yield return new WaitForSeconds(1.5f);
            var rows = new[] { "mira", "aster", "nori" }.All(entity =>
                GameObject.Find("SessionRow_" + entity) != null &&
                GameObject.Find("SessionLast_" + entity) != null &&
                GameObject.Find("SessionTime_" + entity) != null);
            var last = TextOf("SessionLast_mira") == "M1-MIRA-LAST" &&
                       TextOf("SessionLast_aster") == "M1-ASTER-LAST" &&
                       TextOf("SessionLast_nori") == "M1-NORI-LAST";
            var unread = GameObject.Find("SessionUnread_aster") != null;
            var listClickable = Unclickable().All(name => !name.StartsWith("SessionRow_"));
            Shoot(Path.Combine(folder, "1-list.png"));

            var tappedKei = Tap("SessionRow_nori");
            yield return new WaitForSeconds(2.0f);
            var enteredKei = tappedKei && shell.EntityId == "nori" && GameObject.Find("ChatListBack") != null;
            Shoot(Path.Combine(folder, "2-chat-nori.png"));

            var tappedBack = Tap("ChatListBack");
            yield return new WaitForSeconds(0.5f);
            shell.SelfTestShowSessions(SampleSessions());
            yield return new WaitForSeconds(0.5f);
            var returned = tappedBack && GameObject.Find("SessionRow_mira") != null &&
                           GameObject.Find("SessionUnread_aster") != null;
            Shoot(Path.Combine(folder, "3-back-list.png"));

            var tappedRio = Tap("SessionRow_aster");
            yield return new WaitForSeconds(4.0f);
            var enteredRio = tappedRio && shell.EntityId == "aster";
            var cleared = enteredRio && shell.Chat.UnreadFor("aster") == 0;
            Shoot(Path.Combine(folder, "4-chat-aster.png"));
            var pass = rows && last && unread && listClickable && enteredKei && returned && enteredRio && cleared;
            File.WriteAllText(Path.Combine(folder, "result.txt"),
                "pass=" + pass + "\nthree_rows=" + rows + "\nlast_text=" + last +
                "\nunread=" + unread + "\nlist_clickable=" + listClickable +
                "\nentered_nori=" + enteredKei + "\nreturned=" + returned +
                "\nentered_aster=" + enteredRio + "\nunread_cleared=" + cleared +
                "\nunclickable=" + string.Join(" | ", Unclickable()) + "\n");
            Debug.Log("[next-m1] pass=" + pass + " ⇒ " + folder);
            Application.Quit(pass ? 0 : 9);
        }

        private static int M0Count(string marker)
        {
            return FindObjectsByType<TMP_Text>(FindObjectsSortMode.None)
                .Count(text => text.isActiveAndEnabled && text.text.Contains(marker));
        }

        private static float M0Y(string marker)
        {
            var text = FindObjectsByType<TMP_Text>(FindObjectsSortMode.None)
                .FirstOrDefault(row => row.isActiveAndEnabled && row.text.Contains(marker));
            return text != null ? text.transform.position.y : float.MinValue;
        }

        private IEnumerator Page(NextShell shell, string locale, string name, System.Action open)
        {
            open();
            yield return new WaitForSeconds(1.5f);            // 等那一頁的請求回來
            var lines = new StringBuilder();
            foreach (var text in FindObjectsByType<TMP_Text>(FindObjectsSortMode.None))
            {
                if (!text.isActiveAndEnabled || string.IsNullOrWhiteSpace(text.text)) continue;
                var family = text.font != null ? text.font.faceInfo.familyName : "<null>";
                lines.Append(text.gameObject.name).Append('\t').Append(family).Append('\t')
                     .Append(text.text.Replace("\n", "⏎")).Append('\n');
            }
            foreach (var dead in Unclickable()) lines.Append("!unclickable\t").Append(dead).Append('\n');
            // 右上角設定鈕在不在（只該在聊天頁）：Find 只找得到啟用中的物件
            lines.Append("#TopSettings\t").Append(GameObject.Find("TopSettings") != null ? "shown" : "hidden").Append('\n');
            var dir = Path.Combine(_folder, locale);
            Directory.CreateDirectory(dir);
            File.WriteAllText(Path.Combine(dir, name + ".txt"), lines.ToString());
            if (System.Environment.GetEnvironmentVariable("BA_NEXT_UI_SHOTS") == "1")
                Shoot(Path.Combine(dir, name + ".png"));
        }

        /// <summary>
        /// 把畫面拍成 PNG（`BA_NEXT_UI_SHOTS=1`）：畫布是掛在相機前的 WorldSpace ⇒ 讓那台相機畫進
        /// RenderTexture 再讀回來就是整屏（含立繪）。要有圖形裝置：播放器用 `-batchmode`，不要 `-nographics`。
        /// </summary>
        private static void Shoot(string path)
        {
            var camera = UiBuilder.LastCanvasCamera != null ? UiBuilder.LastCanvasCamera : Camera.main;
            if (camera == null) return;
            const int width = ShotWidth;
            const int height = ShotHeight;
            var target = new RenderTexture(width, height, 24);
            var previous = RenderTexture.active;
            camera.targetTexture = target;
            camera.Render();
            RenderTexture.active = target;
            var shot = new Texture2D(width, height, TextureFormat.RGB24, false);
            shot.ReadPixels(new Rect(0, 0, width, height), 0, 0);
            shot.Apply();
            camera.targetTexture = null;
            RenderTexture.active = previous;
            File.WriteAllBytes(path, shot.EncodeToPNG());
            Destroy(shot);
            target.Release();
            Destroy(target);
        }
    }
}
#endif
