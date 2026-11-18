#!/usr/bin/env python3
"""Unity Next 端的 原創幾何風格設計系統（2026-10-29 第二／三期）：守住幾條容易被改回去的規矩。

- 顏色只從 `NextTheme` 拿：頁面不准再寫 `DesignTokens.HexLocal("e3eef8")` 這種散值。
- 按鈕一律走 `NextUi.Button／IconButton`（有按壓回饋），舊的 `PushButton` 不准回來。
- 頁面用到的圖示都有烤好的 PNG；烤圖腳本重跑結果與入庫的檔一致（圖示改了要一起重烤）。
"""

from __future__ import annotations

import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
NEXT = ROOT / "app" / "client" / "BlueAgentUnity" / "Assets" / "Scripts" / "Next"
ICONS = ROOT / "app" / "client" / "BlueAgentUnity" / "Assets" / "Resources" / "icons"
TOOLS = ROOT / "app" / "client" / "tools"

#: 頁面檔（畫面怎麼排在這些檔裡）；Agent 私有管理頁與 TRPG 已在 S4-h 移出手機 App。
VIEWS = ("NextChatView.cs", "NextSettingsView.cs", "NextAccountsView.cs", "NextAlbumView.cs",
         "NextStatusView.cs", "NextLoginView.cs", "NextShell.cs",
         "NextFeedView.cs", "NextComposeView.cs", "NextPostView.cs", "NextBoardUi.cs")


def _code(name: str) -> str:
    return (NEXT / name).read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


class TestDesignRules(unittest.TestCase):
    def test_views_take_colors_from_theme(self):
        offenders = [name for name in VIEWS if "DesignTokens.HexLocal(" in _strip_comments(_code(name))]
        self.assertEqual(offenders, [], "頁面的顏色要從 NextTheme 拿")

    def test_old_push_button_is_gone(self):
        offenders = [path.name for path in NEXT.glob("*.cs") if "PushButton(" in _strip_comments(path.read_text(encoding="utf-8"))]
        self.assertEqual(offenders, [])

    def test_buttons_have_press_feedback(self):
        ui = _code("NextUi.cs")
        for method in ("public static Button Button(", "public static Button IconButton(",
                       "public static Button ListRow(", "public static Button SheetItem("):
            body = ui[ui.index(method):]
            body = body[:body.index("\n        }\n")]
            self.assertIn("AddComponent<NextPress>()", body, method)

    def test_tap_effect_and_background_are_installed(self):
        ui = _code("NextUi.cs")
        page = ui[ui.index("public static Canvas NewPage("):]
        self.assertIn("AddComponent<NextTapFx>()", page[:page.index("\n        }\n")])
        self.assertIn("NextUi.Background(root)", _code("NextShell.cs"))

    def test_every_page_change_animates(self):
        shell = _code("NextShell.cs")
        clear = shell[shell.index("private void ClearBody("):]
        clear = clear[:clear.index("\n        }\n")]
        self.assertIn("_enterPending = true", clear)
        late = shell[shell.index("private void LateUpdate()"):]
        self.assertIn("NextMotion.Enter(", late[:late.index("\n        }\n")])
        # owner 2026-11-07：转场统一覆盖所有页——扫光只在 ClearBody 里，页面自己不叫。
        self.assertIn("if (sweep && !_quietRebuild) Sweep();", clear)
        code = _strip_comments(shell)
        self.assertEqual(code.count("Sweep();"), 1, "Sweep 只能由 ClearBody 呼叫")
        relayout = shell[shell.index("private void Relayout()"):]
        self.assertIn("_quietRebuild = true", relayout[:relayout.index("\n        }\n")])
        render = shell[shell.index("private void RenderSettings()"):]
        self.assertIn("ClearBody(sweep: false)", render[:render.index("\n        }\n")])
        for page in ("ShowFeed", "ShowCompose", "ShowPost", "ShowChatList", "ShowChat",
                     "ShowSettings", "ShowAlbum", "ShowCall", "ShowSubPage"):
            body = shell[shell.index(f"void {page}("):]
            body = body[:body.index("\n        }\n")]
            self.assertRegex(body, r"ClearBody\(\);", page)

    def test_settings_button_only_on_home_pages(self):
        """M3 公共區、M1 會話列表與聊天頁保留設定鈕；其他功能頁收起來（但佔位不動，徽章不跳）。"""
        shell = _strip_comments(_code("NextShell.cs"))
        clear = shell[shell.index("private void ClearBody("):]
        self.assertIn("SetTopButton(_settingsButton, false)", clear[:clear.index("\n        }\n")])
        chat = shell[shell.index("private void ShowChat()"):]
        self.assertIn("SetTopButton(_settingsButton, _loggedIn)", chat[:chat.index("\n        }\n")])
        self.assertEqual(shell.count("_chat.Build("), 1, "聊天頁要一律經過 ShowChat（不然設定鈕不會出來）")
        self.assertEqual(shell.count("SetTopButton(_settingsButton, true)"), 2)
        listing = shell[shell.index("public void ShowChatList()"):]
        self.assertIn("SetTopButton(_settingsButton, true)", listing[:listing.index("\n        }\n")])
        feed = shell[shell.index("public void ShowFeed()"):]
        self.assertIn("SetTopButton(_settingsButton, true)", feed[:feed.index("\n        }\n")])
        # 頂欄聊天入口只在公共区亮；隱藏用透明度，不 SetActive(false)（登入後佔位不變）
        self.assertIn("SetTopButton(_chatButton, true)", feed[:feed.index("\n        }\n")])
        helper = shell[shell.index("private void SetTopButton("):]
        helper = helper[:helper.index("\n        }\n")]
        self.assertIn("group.alpha = visible ? 1f : 0f", helper)

    def test_entity_chip_receives_taps(self):
        """角色切換鈕的底板是 Backdrop（預設不吃點擊）：要明講打開，不然整顆點了沒反應。"""
        chat = _code("NextChatView.cs")
        chip = chat[chat.index("private UnityEngine.UI.Button EntityChip("):]
        chip = chip[:chip.index("\n        }\n")]
        self.assertIn("raycastTarget = true", _strip_comments(chip))

    def test_plus_opens_a_bottom_sheet_not_a_page(self):
        """「+」學 QQ：在打字列下面彈起選單，不直接換頁（owner 2026-10-29）。"""
        chat = _strip_comments(_code("NextChatView.cs"))
        plus = chat[chat.index('IconButton("ChatAlbum"'):]
        plus = plus[:plus.index("\n", plus.index("onClick"))]
        self.assertIn("onClick.AddListener(ToggleSheet)", plus)
        self.assertNotIn("ShowAlbum", plus)
        build = chat[chat.index("private void BuildSheet("):]
        build = build[:build.index("\n        }\n")]
        for target in ("shell.PickImage", "shell.ShowCall"):
            self.assertIn(target, build)
        for removed in ("shell.ShowGame", "shell.ShowTrpg", "shell.ShowDiary", "shell.ShowTasks"):
            self.assertNotIn(removed, build)
        self.assertNotIn("shell.ShowAlbum", build, "相冊牆已搬到設定頁，這裡只保留直接選圖")
        send = chat[chat.index("private void Send(string text)"):]
        self.assertIn("CloseSheet()", send[:send.index("\n        }\n")])

    def test_mic_sits_with_plus_as_an_icon_button(self):
        """麥克風跟「+」同一邊、同一種圓鈕、不帶字（owner 2026-10-29）。"""
        chat = _strip_comments(_code("NextChatView.cs"))
        plus = chat.index('IconButton("ChatAlbum"')
        mic = chat.index('IconButton("ChatMic", composer, "mic", NextUi.Style.Ghost, NextTheme.TapSize, round: true)')
        slot = chat.index('UiBuilder.Row("ChatInputSlot"')
        send = chat.index('NextUi.Button("ChatSend"')
        self.assertLess(plus, mic)
        self.assertLess(mic, slot)
        self.assertLess(slot, send)

    def test_hold_to_talk_records_but_only_send_sends(self):
        """按住錄、放開停（不送），按「送信」才送（owner 2026-10-29）。"""
        chat = _strip_comments(_code("NextChatView.cs"))

        def body(signature):
            part = chat[chat.index(signature):]
            return part[:part.index("\n        }\n")]

        self.assertIn("voice.BeginRecording()", body("private void HoldDown()"))
        up = body("private void HoldUp()")
        self.assertIn("voice.Stop(", up)
        self.assertIn("_pendingWav = wav", up)
        self.assertNotIn("SendAudio", up)
        send = body("private void OnSendPressed()")
        self.assertIn("SendAudio(", send)
        self.assertIn("voice_hold_first", send)
        hold = body("private void BuildHold(")
        self.assertIn("AddComponent<NextHold>()", hold)
        self.assertIn("raycastTarget = true", hold)

    def test_album_grid_and_pagination_scroll_together(self):
        """一页 30 张就是十行；照片牆与加载更多都必须在滚动容器里。"""
        album = _strip_comments(_code("NextAlbumView.cs"))
        self.assertIn('NextUi.ScrollArea("AlbumScroll"', album)
        self.assertIn("gridRect.SetParent(scrollContent, false)", album)
        self.assertIn('NextUi.Button("AlbumMore", scrollContent', album)

    def test_rebuild_stops_old_recording_before_resetting_flags(self):
        """Destroy 在帧尾才触发 OnDisable；切角色重建前必须先停旧麦克风。"""
        chat = _strip_comments(_code("NextChatView.cs"))
        build = chat[chat.index("public void Build(RectTransform body"):]
        build = build[:build.index("var twoPane = NextLayout.TwoPane(")]
        self.assertIn("if (_recording) AbortRecording();", build)
        self.assertNotIn("_recording = false", build)
        self.assertLess(build.index("AbortRecording()"), build.index("ResetForEntity("))

    def test_voice_resend_rules(self):
        """送語音後的錄音去留（review 2026-10-29）：
        每次送都是新的一顆泡泡（不把新錄音寫進舊泡泡）；server 收下（accepted）就清錄音；
        收尾時只有「server 明確拒收」才留錄音並拿掉這次的泡泡，斷在半路就丟錄音（避免重複一輪）。
        拒收與否的判斷在 Logic 的 NextText.ServerRejected（dotnet test 釘行為）。"""
        chat = _strip_comments(_code("NextChatView.cs"))

        def body(signature):
            part = chat[chat.index(signature):]
            return part[:part.index("\n        }\n")]

        send = body("private void OnSendPressed()")
        self.assertIn('_voiceBubble = Append("…", true,', send)
        self.assertNotIn("if (_voiceBubble == null)", send, "重送不能沿用上一顆泡泡")
        accepted = chat[chat.index('case "accepted":'):chat.index('case "thinking":')]
        self.assertIn("if (_sendingVoice)", accepted, "圖片／文字的 accepted 不能清待送語音")
        self.assertIn("_pendingWav = null", accepted)
        settle = body("private void SettleVoiceAfterClose(string failure)")
        self.assertIn("NextText.ServerRejected(failure)", settle)
        self.assertIn("DropMessage(_voiceBubble)", settle)
        self.assertIn("else _pendingWav = null", settle)
        self.assertIn("_voiceBubble = null", settle)
        self.assertIn("SettleVoiceAfterClose(failure)", body("private void OnClosed(TurnTicket"))
        # 換回打字／重錄都不會留下舊泡泡：泡泡只在 SettleVoiceAfterClose 與 done 時放手
        mode = body("private void SetVoiceMode(bool voice)")
        self.assertIn("_pendingWav = null", mode)

    def test_no_arming_band_aid_or_press_log(self):
        """麥克風一啟動就開的根因是 NextVoice.Start() 被當生命週期呼叫（已改名）；
        NextHold 不再需要「武裝」，HoldDown 也不再每按一次就打 log。"""
        motion = _strip_comments(_code("NextMotion.cs"))
        hold = motion[motion.index("public sealed class NextHold"):]
        hold = hold[:hold.index("\n    }\n")]
        self.assertNotIn("_armed", hold)
        chat = _strip_comments(_code("NextChatView.cs"))
        down = chat[chat.index("private void HoldDown()"):]
        down = down[:down.index("\n        }\n")]
        self.assertNotIn("Debug.Log", down)
        self.assertNotIn("IsMaster", down, "guest 也可以語音輸入（accounts-and-clients.md §1）")

    def test_settings_tabs_do_not_sweep(self):
        """切頁籤只重畫設定頁，不掃光（掃光只給大的換頁）。"""
        shell = _code("NextShell.cs")
        page = shell[shell.index("public void ShowSettingsPage(string page)"):]
        page = page[:page.index("\n        }\n")]
        self.assertIn("RenderSettings()", page)
        self.assertNotIn("ShowSettings()", page)


class TestIcons(unittest.TestCase):
    #: 頁面裡以字串常值指名的圖示（第二個參數是圖示名的那幾支）
    CALLS = (
        re.compile(r'UiBuilder\.Icon\(\s*"[^"]*"\s*,\s*"([a-z-]+)"'),
        re.compile(r'IconButton\(\s*"[^"]*"\s*,\s*\w+\s*,\s*"([a-z-]+)"'),
        re.compile(r'(?:ListRow|SheetItem)\(\s*"[^"]*"\s*,\s*\w+\s*,\s*"([a-z-]+)"'),
        re.compile(r'Item\(\s*"Sheet\w+"\s*,\s*"([a-z-]+)"'),                         # 底部選單的格子
        re.compile(r'\(\s*\w+\s*,\s*NextStrings\.Get\("[a-z_]+"\)\s*,\s*"([a-z-]+)"\s*\)'),   # 頁籤 (key, label, icon)
        re.compile(r'Section\([^;]*?,\s*"([a-z-]+)"\s*\)\s*;'),
        re.compile(r'Button\([^;]*?NextUi\.Style\.\w+[^;]*?,\s*"([a-z-]+)"'),
    )

    def test_every_named_icon_is_baked(self):
        used = set()
        for path in NEXT.glob("*.cs"):
            text = _strip_comments(path.read_text(encoding="utf-8"))
            for pattern in self.CALLS:
                used |= set(pattern.findall(text))
        self.assertGreater(len(used), 15)
        missing = sorted(name for name in used if not (ICONS / f"i-{name}.png").exists())
        self.assertEqual(missing, [])

    def test_baked_icons_match_the_generator(self):
        sys.path.insert(0, str(TOOLS))
        try:
            import bake_next_icons as bake
        finally:
            sys.path.pop(0)
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            for name in ("mic", "send", "halo"):
                spec = bake.ICONS.get(name)
                alpha = (bake.rasterize(spec, bake.STROKE) if spec is not None
                         else bake.rasterize(bake.DECOR[name]["stroke"], bake.DECOR[name]["width"], bake.DECOR[name]["fill"]))
                fresh = pathlib.Path(folder) / f"i-{name}.png"
                bake.write_png(fresh, alpha)
                self.assertEqual(fresh.read_bytes(), (ICONS / f"i-{name}.png").read_bytes(),
                                 f"i-{name}.png 跟產生器對不上：python3 app/client/tools/bake_next_icons.py 重烤")


class TestRecordingSeconds(unittest.TestCase):
    """錄音顯示的秒數必須等於實際錄到的長度（2026-10-29 修）。

    原本 `NextVoice.Seconds` 用牆鐘時間（`realtimeSinceStartup - _startedAt`），
    而 `Stop()` 裁資料用 `Microphone.GetPosition()`（實際樣本數）⇒ 兩套來源脫節，
    出現「按住 3 秒顯示 60.0s」。修法：兩邊都走 `RecordedFrames()`。
    """

    def test_seconds_come_from_recorded_frames_not_wall_clock(self):
        voice = _code("NextVoice.cs")
        body = voice[voice.index("public float Seconds"):]
        body = body[:body.index("\n        }\n")]
        self.assertIn("RecordedFrames()", body,
                      "錄音中的秒數要來自 RecordedFrames()（實際樣本數）")
        self.assertNotIn("realtimeSinceStartup", body,
                         "秒數不可用牆鐘時間算（會與實際錄到的長度脫節）")

    def test_recorded_frames_delegates_to_voice_frames(self):
        """範圍規則（緩衝全長、牆鐘＋餘量、未錄滿的 0）在 Logic 的 VoiceFrames，行為由 VoiceFramesTest 釘；
        這裡只確認 NextVoice 真的走它，Stop 也只用這一個數。"""
        voice = _code("NextVoice.cs")
        body = voice[voice.index("private int RecordedFrames()"):]
        body = body[:body.index("\n        }\n")]
        self.assertIn("Microphone.GetPosition", body)
        self.assertIn("VoiceFrames.Recorded(", body)
        stop = voice[voice.index("public byte[] Stop(out float seconds)"):]
        self.assertIn("var position = RecordedFrames();", stop[:stop.index("\n        }\n")])

    def test_stop_uses_recorded_frames_and_never_falls_back_to_clip_samples(self):
        voice = _code("NextVoice.cs")
        body = voice[voice.index("public byte[] Stop(out float seconds)"):]
        body = body[:body.index("\n        /// <summary>線性重採樣")]
        self.assertIn("RecordedFrames()", body, "Stop 要用 RecordedFrames()")
        self.assertNotIn(": clip.samples", body,
                         "GetPosition 拿不到時不可拿 clip.samples 當實際長度（＝整個 60 秒緩衝）")


class TestMicOnlyWhenUserPresses(unittest.TestCase):
    """麥克風只能在使用者**主動按下去**時開（2026-10-29 owner）。

    實測 bug：app 一啟動、**一根手指都沒碰**，麥克風就已經在錄
    （logcat `[next-voice] 録音開始`；appops `RECORD_AUDIO … (running)`）。
    根因：`NextVoice` 有個叫 `Start()` 的開錄方法，Unity 把它當生命週期回調自動呼叫（ed320f2 改名
    `BeginRecording`；見 `test_no_lifecycle_name_collisions`）。一度懷疑是 UGUI 第一幀的殘留指標狀態、
    加過「武裝」機制——那是猜錯的，review 2026-10-29 拿掉了。
    """

    def test_hold_down_requires_voice_mode(self):
        chat = _code("NextChatView.cs")
        body = chat[chat.index("private void HoldDown()"):]
        body = body[:body.index("\n        }\n")]
        self.assertIn("if (!_voiceMode) return;", body,
                      "不是語音模式就不准開錄（打字模式那顆是隱藏的，但要有這道閘）")

    def test_next_app_does_not_use_the_ear_pipeline(self):
        """Next 端不准接舊殼的耳朵鏈（`EarsLink`／`MicCapture`／`WakeNet`）——
        那是「一直聽 + 聲紋 + 激活詞」那套；Next 是「按住才錄」。"""
        offenders = []
        for path in NEXT.glob("*.cs"):
            text = _strip_comments(path.read_text(encoding="utf-8"))
            for token in ("EarsLink", "WakeNet", "WakeEngine", "WakeKws"):
                if token in text:
                    offenders.append(f"{path.name}: {token}")
        self.assertEqual(offenders, [], "Next 端不該接耳朵／喚醒網那條鏈")

    def test_logout_drops_the_call_retire_wait(self):
        """通話中登出／token 失效（2026-10-29 review）：殼先 `HangUpActive()` 再 `Session.Close()`；
        Close 之後那一段登入的回包全被丟掉，`CompleteRetire` 永遠不會來 ⇒ `HangUpActive` 要自己放下
        `_retiring`／`_afterRetire`，不然重新登入後再撥會一直卡在「発信中…」。"""
        call = _strip_comments(_code("NextCallView.cs"))
        hang = call[call.index("public static void HangUpActive()"):]
        hang = hang[:hang.index("\n        }\n")]
        self.assertIn("_retiring = null", hang)
        self.assertIn("_afterRetire = null", hang)
        shell = _strip_comments(_code("NextShell.cs"))
        login = shell[shell.index("private void ShowLogin(string message)"):]
        self.assertLess(login.index("NextCallView.HangUpActive()"), login.index("Api.Session.Close()"),
                        "要趁 token 還在先掛")

    def test_no_lifecycle_name_collisions(self):
        """⚠️ **Unity 的訊息機制只看方法名、不看回傳型別** ⇒ `public bool Start()`
        也會被當成生命週期回調自動呼叫。

        2026-10-29 真踩：`NextVoice.Start()`（開錄）被 Unity 自動呼叫 ⇒
        app 一啟動麥克風就開了（一根手指都沒碰）。
        ⇒ `Scripts/Next` 底下不准有跟生命週期同名的公開方法
        （`Awake`／`Start`／`Update`／`OnEnable`／`OnDisable`／`OnDestroy`／`OnGUI`／`OnApplicationPause`）。
        """
        lifecycle = {"Awake", "Start", "Update", "OnEnable", "OnDisable", "OnDestroy",
                     "OnGUI", "OnApplicationPause", "OnApplicationFocus", "LateUpdate",
                     "FixedUpdate", "OnDestroy"}
        offenders = []
        for path in NEXT.glob("*.cs"):
            source = path.read_text(encoding="utf-8")
            # 只看程式碼本身（註解裡提到 `Start()` 不算）
            source = "\n".join(line.split("//", 1)[0] for line in source.splitlines())
            for name in lifecycle:
                # 跳過合法生命週期實作（`public void OnDisable()` 之類）
                for match in re.finditer(
                        rf"public\s+[\w<>\[\]\.]+\s+{name}\s*\(", source):
                    tail = source[match.start():match.start() + 120]
                    if re.match(rf"public\s+void\s+{name}\s*\(\s*\)", tail):
                        continue
                    offenders.append(f"{path.name}: public … {name}(…)")
        self.assertEqual(offenders, [], "會被 Unity 當成生命週期自動呼叫的方法名")


class TestStickerTrayHeight(unittest.TestCase):
    """表情盤的網格高度必須**自己釘死**（2026-10-29 真機實測：面板整片空白）。

    根因是**循環依賴**：捲動區 content（`VerticalLayoutGroup` ＋ `ContentSizeFitter`）的高度
    來自 grid 的 preferredHeight；而 `GridLayoutGroup` 的 preferredHeight 又要它自己先被
    佈局過才算得出來 ⇒ 兩者互相等，結果高度＝0。

    （貼圖全都下載成功——伺服器端 105 個請求全是 200——所以不是下載的問題。）
    相片牆沒這個毛病：它的 content 下面還有一顆分頁鈕撐著。
    """

    def test_grid_height_is_pinned_explicitly(self):
        stickers = _code("NextStickers.cs")
        self.assertIn("PinGridHeight", stickers,
                      "必須有把網格高度釘死的方法（不能靠 UGUI 迭代求解）")
        body = stickers[stickers.index("private void PinGridHeight(int count)"):]
        body = body[:body.index("\n        }\n")]
        self.assertIn("LayoutElement", body, "要釘進 LayoutElement")
        self.assertIn("preferredHeight", body, "要設定 preferredHeight")

    def test_pinned_height_uses_the_same_constants_as_the_grid(self):
        """算高度用的格數／格寬／間距，必須跟網格本身用的是同一組常數（不能各寫一份）。"""
        stickers = _code("NextStickers.cs")
        self.assertIn("private const int Columns = 5;", stickers)
        self.assertIn("private const float Cell = 72f;", stickers)
        self.assertIn("private const float Spacing = 10f;", stickers)
        # 網格本身也要用常數，不能寫死字面值
        self.assertIn("layout.constraintCount = Columns;", stickers)
        self.assertIn("layout.spacing = new Vector2(Spacing, Spacing);", stickers)
        self.assertIn("layout.cellSize = new Vector2(Cell, Cell);", stickers)

    def test_download_queue_resets_on_new_session(self):
        """換工作階段時舊請求的回呼會被 `Settle` 丟掉 ⇒ 名額不重置的話佇列會永久卡住（複檢 2026-10-29）。"""
        stickers = _code("NextStickers.cs")
        body = stickers[stickers.index("private static void Enqueue("):]
        body = body[:body.index("\n        }\n")]
        self.assertIn("Session.Epoch", body, "佇列要認得是哪一個工作階段")
        self.assertIn("_inFlight = 0;", body, "換階段要把在飛的名額歸零")

    def test_scroll_to_top_uses_one(self):
        """ScrollRect 的 verticalNormalizedPosition：1 才是最上面。"""
        stickers = _code("NextStickers.cs")
        body = stickers[stickers.index("private void ScrollToTop()"):]
        body = body[:body.index("\n        }\n")]
        self.assertIn("verticalNormalizedPosition = 1f", body)


class TestAgentPrivatePagesRemoved(unittest.TestCase):
    """S4-h：日記、任務、棋局與 TRPG 不是手機社交平台的功能。"""

    def test_views_navigation_and_legacy_api_calls_are_gone(self):
        for name in ("NextDiaryView.cs", "NextTasksView.cs", "NextGameView.cs", "NextTrpgView.cs"):
            self.assertFalse((NEXT / name).exists(), name)
        client = "".join(_code(name) for name in ("NextShell.cs", "NextChatView.cs", "NextApi.cs", "NextUiDump.cs"))
        for removed in ("ShowDiary", "ShowTasks", "ShowGame", "ShowTrpg", "OnTableClosedByHer",
                        "/api/game", "/api/trpg/", "trpg_closed"):
            self.assertNotIn(removed, client)


class TestMediaFullOverlay(unittest.TestCase):
    """全螢幕看圖要蓋住整個畫面（2026-10-29 模擬器實測：以前排在相冊下面、只露出下半截）。"""

    def test_overlay_is_out_of_layout_on_the_canvas(self):
        shell = _code("NextShell.cs")
        body = shell[shell.index("public void ShowMediaFull("):]
        body = body[:body.index("\n        }\n")]
        self.assertIn('UiBuilder.Overlay("MediaFull", _canvas.transform)', body)
        self.assertNotIn("UiBuilder.Column(", body, "不能當成版面裡的一欄")


class TestUsabilityPass(unittest.TestCase):
    """UI 易用性一輪（owner 2026-10-30「UI 都按照你說的改」）：每一條釘一個最小的結構判據。"""

    @staticmethod
    def _body(source, signature):
        part = source[source.index(signature):]
        return part[:part.index("\n        }\n")]

    def test_status_bar_is_gone_and_notices_float(self):
        shell = _strip_comments(_code("NextShell.cs"))
        chrome = self._body(shell, "private void BuildChrome()")
        self.assertNotIn('"StatusBar"', chrome, "底部狀態列拿掉（輸入框整體下移）")
        self.assertIn("NextToast.Show(_bodyHost, text);", self._body(shell, "public void Status(string text)"))
        badge = self._body(shell, "private void SetConnBadge(string key)")
        self.assertIn('_loggedIn && key == "badge_reconnecting"', badge, "斷線才掛橫幅")

    def test_text_and_tap_targets_are_bigger(self):
        theme = _code("NextTheme.cs")
        import re
        sizes = {name: float(value) for name, value in
                 re.findall(r"public const float (\w+Size) = ([0-9.]+)f;", theme)}
        self.assertGreaterEqual(sizes["BodySize"], 15)
        self.assertGreaterEqual(sizes["TinySize"], 11)
        self.assertGreaterEqual(sizes["TapSize"], 42)
        chat = _code("NextChatView.cs")
        self.assertNotIn("Ghost, 36f", chat)

    def test_input_is_multi_line_and_grows(self):
        chat = _strip_comments(_code("NextChatView.cs"))
        self.assertIn("LineType.MultiLineNewline", chat)
        self.assertNotIn("onSubmit.AddListener", chat, "Return 換行，按送信才送")
        self.assertIn("InputMaxHeight", self._body(chat, "private void FitInput()"))

    def test_long_press_copies(self):
        chat = _strip_comments(_code("NextChatView.cs"))
        self.assertIn("AddComponent<NextLongPress>()", self._body(chat, "private RectTransform BuildBubble("))
        self.assertIn("GUIUtility.systemCopyBuffer", self._body(chat, "private void CopyMessage("))
        press = _code("NextMotion.cs")
        press = press[press.index("public sealed class NextLongPress"):]
        self.assertIn("_press.dragging", press, "拖動＝在捲，不算長按")
        self.assertNotIn("IDragHandler", press[:press.index("{")], "不能吃拖曳，捲動要交給 ScrollRect")

    def test_slide_up_cancels_recording(self):
        chat = _strip_comments(_code("NextChatView.cs"))
        up = self._body(chat, "private void HoldUp()")
        self.assertLess(up.index("_holdCancel"), up.index("voice.Stop(out seconds)"))
        self.assertIn("hold.Moved = HoldMoved;", chat)

    def test_new_messages_do_not_yank_the_reader(self):
        chat = _strip_comments(_code("NextChatView.cs"))
        follow = self._body(chat, "private void FollowBottom(bool force = false)")
        self.assertIn("NearBottom()", follow)
        self.assertIn("ShowNewPill(true)", follow)
        self.assertEqual(chat.count("NextUi.StickToBottom(_scroll)"), 1, "只剩 FollowBottom 裡那一處")
        send = self._body(chat, "private void Send(string text)")
        self.assertIn("FollowBottom(true)", send, "自己送的一定拉到底")

    def test_older_history_loads_on_scroll_to_top(self):
        chat = _strip_comments(_code("NextChatView.cs"))
        older = self._body(chat, "private void LoadOlder()")
        self.assertIn("Chat.Messages(entity, OlderPage, _oldestTurn)", older)
        self.assertNotIn("GetTurns(", older, "S3 補舊只讀本地 SQLite，不重新向 Agent 拿 turns")
        prepend = self._body(chat, "private int PrependHistory(")
        self.assertIn("RenderTurns(entity, turns, false)", prepend, "補舊的不推游標")
        self.assertIn("KeepAnchor(anchor, anchorY)", prepend, "畫面停在原來看的那一則")
        # 拖動中不插（ScrollRect 拖動時每幀直接設位置，補的位移會被蓋掉 ⇒ 模擬器實測畫面跳走）
        gate = self._body(chat, "private void MaybeLoadOlder()")
        self.assertIn("Input.touchCount > 0 || Input.GetMouseButton(0)", gate)
        self.assertIn("MaybeLoadOlder();", self._body(chat, "public void Tick()"))

    def test_times_and_day_separators(self):
        chat = _strip_comments(_code("NextChatView.cs"))
        append = self._body(chat, "private Msg Append(string text, bool mine")
        self.assertIn("MaybeDaySeparator(at);", append)
        self.assertIn('"BubbleTime"', self._body(chat, "private RectTransform BuildBubble("))
        render = self._body(chat, "private int RenderTurns(")
        self.assertIn('row.OptDouble("at")', render, "歷史用伺服器記的時間，不是載入的時間")

    def test_found_by_the_full_page_dump(self):
        """全頁截圖順手抓到的三個（2026-10-30）：設定的鍵值行被撐開、狀態頁回包壓到標題、開選單誤浮「新訊息」。"""
        ui = _code("NextUi.cs")
        kv = ui[ui.index("public static Component KeyValue("):]
        kv = kv[:kv.index("\n        }\n")]
        self.assertIn("element.flexibleHeight = 0f;", kv)
        status = _code("NextStatusView.cs")
        show = status[status.index("private static void Show(Component detail, string text)"):]
        self.assertIn("UiBuilder.Settle(", show[:show.index("\n        }\n")])
        chat = _strip_comments(_code("NextChatView.cs"))
        sheet = self._body(chat, "private void AnimateSheet(")
        self.assertIn("var wasAtBottom = NearBottom();", sheet)

    def test_resend_only_when_server_never_took_it(self):
        chat = _strip_comments(_code("NextChatView.cs"))
        closed = self._body(chat, "private void OnClosed(TurnTicket ticket, string failure)")
        self.assertIn("string.IsNullOrEmpty(_currentTurn) && string.IsNullOrEmpty(_pendingTurn)", closed)
        self.assertIn("OfferResend(", closed)


class TestNetworkSlim(unittest.TestCase):
    """網路傳輸瘦身（owner 2026-10-30「先做你說的 5 項」）的端末那一側。"""

    def test_events_watchdog_and_backoff_reset(self):
        api = _strip_comments(_code("NextApi.cs"))
        loop = api[api.index("private IEnumerator EventsRoutine("):]
        loop = loop[:loop.index("public void StopEvents()")]
        self.assertIn("EventsSilenceLimit", loop, "推播沒動靜就自己斷、重連")
        self.assertIn("request.Abort();", loop)
        # 變數名從 wire 改成 frame（2026-11-04 逐幀派送改走 SseFramePump）⇒ 只認事件名，不認變數名
        self.assertIn("backoff = 1f;", loop[loop.index('Event == "ready"'):], "連上就把退避歸零")

    def test_downloads_share_one_limit(self):
        api = _strip_comments(_code("NextApi.cs"))
        routine = api[api.index("private IEnumerator BytesRoutine("):]
        routine = routine[:routine.index("private IEnumerator FetchBytes(")]
        self.assertIn("_bytesInFlight >= MaxBytesInFlight", routine)
        self.assertIn("finally", routine, "名額一定要還（中途丟例外也一樣）")

    def test_album_and_tray_ask_for_thumbnails(self):
        self.assertIn("NextApi.ThumbWidth", _code("NextAlbumView.cs"))
        stickers = _code("NextStickers.cs")
        self.assertIn('"?w=" + TrayThumb', stickers)
        self.assertIn('hash + "-w" + TrayThumb', stickers, "縮圖雜湊從清單推，照樣不必問")

    def test_upload_declares_the_real_image_type(self):
        api = _code("NextApi.cs")
        self.assertIn('request.SetRequestHeader("Content-Type", ImageType(image));', api)
        self.assertNotIn('"Content-Type", "image/png"', api)


class TestComposerInput(unittest.TestCase):
    """聊天輸入欄（owner 2026-10-30「看不見 cursor、不能移動；長文時兩側的鈕垂直置中很難看」）。"""

    def test_caret_is_drawn_and_movable(self):
        builder = _strip_comments((NEXT.parent / "UiBuilder.cs").read_text(encoding="utf-8"))
        body = builder[builder.index("public static TMP_InputField Input("):]
        body = body[:body.index("return field;")]
        # 每一格都要（聊天欄、登入 token 欄）：不藏系統輸入列 ⇒ 手機上 TMP 不就地編輯、**不畫游標**
        self.assertIn("field.shouldHideMobileInput = true;", body)
        chat = _strip_comments(_code("NextChatView.cs"))
        self.assertIn("_input.onFocusSelectAll = false;", chat, "點進來放游標，不是全選")

    def test_side_buttons_sit_at_the_bottom(self):
        chat = _strip_comments(_code("NextChatView.cs"))
        line = chat[chat.index('UiBuilder.Row("ChatComposer"'):]
        self.assertIn("TextAnchor.LowerCenter", line[:line.index(";")], "長文撐高時兩側鈕貼底")

    def test_long_text_does_not_widen_the_field(self):
        chat = _strip_comments(_code("NextChatView.cs"))
        self.assertIn("_slotHeight.preferredWidth = 0f;", chat, "欄位只吃剩下的寬，送信鈕不被擠扁")


class TestLargeScreen(unittest.TestCase):
    """iPhone Duo 展開屏（owner 2026-10-30）：內屏天生是橫的，以前鎖直向 ⇒ 被縮成中間一條。"""

    def test_build_declares_landscape_and_runtime_narrows_it(self):
        build = (NEXT.parents[1] / "Editor" / "HeadlessBuild.cs").read_text(encoding="utf-8")
        body = build[build.index("private static void BuildNext("):]
        body = body[:body.index("BuildPipeline.BuildPlayer(")]
        # 一定要在 ConfigureIOS() 之後（它會把方向鎖回直向；第一版就是這樣被蓋掉的）
        self.assertGreater(body.index("AllowLargeScreenRotation();"), body.index("ConfigureIOS();"))
        helper = build[build.index("private static void AllowLargeScreenRotation()"):]
        helper = helper[:helper.index("\n        }\n")]
        self.assertIn("UIOrientation.AutoRotation", helper)
        self.assertIn("allowedAutorotateToLandscapeLeft = true", helper)
        layout = _code("NextLayout.cs")
        self.assertIn("AllowLandscape(IsLargeScreen());", layout, "只有大螢幕放開橫向")
        self.assertIn("ba_screen_short_side", layout, "iOS 要問整塊螢幕，不能用被縮成一條的 Screen")
        shell = _code("NextShell.cs")
        self.assertIn("NextLayout.ApplyOrientation();", shell[shell.index("private void Start()"):][:300])
        # 摺著開 app 再展開：縮著的 app 量不到內屏 ⇒ 尺寸一變先放開橫向試探，等一下才收（模擬器實測）
        watch = shell[shell.index("private void WatchScreen()"):]
        watch = watch[:watch.index("\n        }\n")]
        self.assertLess(watch.index("NextLayout.AllowLandscape(true);"), watch.index("NextLayout.ApplyOrientation();"))
        self.assertIn("_quietNextChange", watch, "自己收橫向造成的那次變化不能再試探（會來回翻）")

    def test_canvas_follows_the_screen(self):
        self.assertIn("UiBuilder.DesignWidth = NextLayout.DesignWidth();", _code("NextUi.cs"))
        shell = _code("NextShell.cs")
        relayout = shell[shell.index("private void Relayout()"):]
        relayout = relayout[:relayout.index("\n        }\n")]
        for piece in ("UiBuilder.FitToCamera(root, _camera);", "_rebuild();"):
            self.assertIn(piece, relayout)
        self.assertIn("CanvasWidth / Screen.width", shell, "鍵盤讓位不能再寫死 460")

    def test_chat_splits_into_two_panes_when_wide(self):
        chat = _code("NextChatView.cs")
        self.assertIn("NextLayout.TwoPane(shell.CanvasWidth)", chat)
        self.assertIn('NextStageView.Build(side,', chat)
        self.assertIn('UiBuilder.Row("ChatHead", side,', chat)

    def test_only_safe_pages_rebuild_on_resize(self):
        """通話重畫會重撥 ⇒ 不登記重畫，只重排。"""
        shell = _code("NextShell.cs")
        body = shell[shell.index("public void ShowCall()"):]
        body = body[:body.index("\n        }\n")]
        self.assertNotIn("_rebuild =", body)
        clear = shell[shell.index("private void ClearBody("):]
        self.assertIn("_rebuild = null;", clear[:600])


class TestCallMarkerEverywhere(unittest.TestCase):
    """通話標記在**每一條**畫訊息的路上都要是置中小字（複檢 2026-10-30）。

    603fb0e 只改了「進聊天頁讀歷史」那條；回到聊天頁的重畫（`Refresh`）和
    補拿（`SyncMissed`，掛斷後的標記多半走這條）還把它畫成她的泡泡。
    """

    @staticmethod
    def _body(name):
        chat = _code("NextChatView.cs")
        body = chat[chat.index(name):]
        return body[:body.index("\n        }\n")]

    def test_history_refresh_and_catch_up_all_know_the_marker(self):
        self.assertIn("RenderTurns(entity, turns, true)", self._body("private int PaintHistory("))
        self.assertIn('"call-log"', self._body("private int RenderTurns("))
        self.assertIn("msg.Marker ? BuildMarker(msg.Text)", self._body("private void Refresh()"))
        # 語音仍走舊 Agent 能力；其通話標記的斷線補拿必須保留，但文本 SyncMissed 不再走這條。
        catch_up = self._body("private void LegacySyncMissed()")
        self.assertIn('"call-log"', catch_up)
        self.assertIn("AppendMarker(CallMarkerText(row), turn, row.OptDouble(\"at\"))", catch_up)
        self.assertIn("if (!marker) recovered++;", catch_up, "標記不算補回來的話")
        platform = self._body("public void SyncMissed()")
        self.assertIn("ChatSync.SyncConversation", platform)
        self.assertNotIn("GetTurnsSince", platform)


class TestAppSideCache(unittest.TestCase):
    """App 側快取（owner 2026-10-29「對比線上的雜湊決定要不要重新同步」）：接線釘在這裡，規則在 `SyncCacheTest`。"""

    def test_cache_is_per_account_and_dropped_on_logout(self):
        shell = _code("NextShell.cs")
        login = shell[shell.index("Api.Cursors.Bind(Api.BaseUrl, Principal);"):]
        self.assertIn("Api.BindCache(Principal, ChatAccountId);", login[:200],
                      "登入沿用這個人的快取槽，會話使用 canonical account_id")
        logout = shell[shell.index("Api.Cursors.Unbind();"):]
        self.assertIn('Api.BindCache("");', logout[:200], "登出就不再指向他")

    def test_cache_lives_where_the_os_may_purge_it(self):
        api = _code("NextApi.cs")
        self.assertIn("Application.temporaryCachePath", api)
        self.assertNotIn("persistentDataPath, \"next-sync\"", api, "不進 iCloud 備份")

    def test_history_uses_persistent_store_and_tray_still_uses_etag_cache(self):
        chat = _code("NextChatView.cs")
        body = chat[chat.index("private void LoadRecent()"):]
        body = body[:body.index("\n        }\n")]
        self.assertLess(body.index("PaintLocalLatest"), body.index("SyncConversation"),
                        "先畫 SQLite 完整副本，再背景對賬")
        for old in ("CachedJson", "GetTurns(", "reply.NotModified", "StoreJson"):
            self.assertNotIn(old, body, "S3 文本歷史不再依賴臨時 ETag 快取或 Agent turns")
        stickers = _code("NextStickers.cs")
        body = stickers[stickers.index("private void Load()"):]
        body = body[:body.index("\n        }\n")]
        self.assertLess(body.index("CachedJson"), body.index("GetStickers("))
        self.assertIn("reply.NotModified", body)

    def test_cache_does_not_depend_on_the_etag_header(self):
        """回應被代理丟了 `ETag` 也要存得對（不然每次都當「換過了」整包重抓）。"""
        api = _code("NextApi.cs")
        body = api[api.index("private IEnumerator FetchBytes("):]
        body = body[:body.index("\n        }\n")]
        self.assertIn("SyncCache.ContentTag(data)", body, "沒有 ETag 就自己算")
        store = api[api.index("public void StoreJson("):]
        store = store[:store.index("\n        }\n")]
        self.assertIn("reply.Json.OptString(tagField)", store, "沒有 ETag 就用本文裡的雜湊欄位")
        self.assertIn('StoreJson(key, reply, "version")', _code("NextStickers.cs"))
        self.assertIn('System.IO.Path.Combine(Application.persistentDataPath, "next-social", folder)', api,
                      "聊天真副本持久化；只有貼圖等可重建資料繼續使用臨時快取")

    def test_sticker_hash_skips_the_request(self):
        stickers = _code("NextStickers.cs")
        body = stickers[stickers.index("public static void Paint("):]
        body = body[:body.index("\n        }\n")]
        self.assertIn("Cache.Fresh(url, hash)", body)
        self.assertLess(body.index("CachedBytes"), body.index("Enqueue("), "本機對得上就不排隊下載")


if __name__ == "__main__":
    unittest.main(verbosity=2)
