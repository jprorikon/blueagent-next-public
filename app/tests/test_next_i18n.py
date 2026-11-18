#!/usr/bin/env python3
"""Unity Next 端的完整 i18n（2026-10-28）：語言檔與「不准寫死中日文」的閘門。

閘門本身是 `app/client/tools/check_next_strings.py`（建 APK 前也會跑）；這裡跑它一次，
再對它的每一條規則各餵一個壞例子——確認閘門真的會紅，不是永遠綠。
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TOOLS = ROOT / "app" / "client" / "tools"
NEXT = ROOT / "app" / "client" / "BlueAgentUnity" / "Assets" / "Scripts" / "Next"
I18N = ROOT / "app" / "client" / "BlueAgentUnity" / "Assets" / "Resources" / "i18n"

sys.path.insert(0, str(TOOLS))
import check_next_strings as gate  # noqa: E402
sys.path.pop(0)


class TestGateOnRepo(unittest.TestCase):
    def test_gate_passes_on_repo(self):
        result = subprocess.run([sys.executable, str(TOOLS / "check_next_strings.py")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_three_locales_share_keys(self):
        tables = {locale: json.loads((I18N / f"next_{locale}.json").read_text(encoding="utf-8"))
                  for locale in gate.LOCALES}
        self.assertEqual(set(tables["ja"]), set(tables["zh-Hans"]))
        self.assertEqual(set(tables["ja"]), set(tables["zh-Hant"]))
        self.assertGreater(len(tables["ja"]), 150)

    def test_chinese_tables_have_no_kana(self):
        """中文介面不該冒出假名（以前就是這樣混的）。"""
        import re
        kana = re.compile(r"[぀-ヿ]")
        for locale in ("zh-Hans", "zh-Hant"):
            table = json.loads((I18N / f"next_{locale}.json").read_text(encoding="utf-8"))
            leaked = sorted(key for key, value in table.items() if kana.search(value))
            self.assertEqual(leaked, [], locale)

    def test_language_switch_refreshes_persistent_top_bar(self):
        """頂欄只建一次：換語言時連線／身份兩顆徽章、狀態列要重套（無頭倒字抓到過頂欄留日語）。
        （2026-10-29 改版後設定鈕只有圖示，沒有字要換。）"""
        shell = (NEXT / "NextShell.cs").read_text(encoding="utf-8")
        cycle = shell[shell.index("public void CycleLanguage()"):]
        cycle = cycle[:cycle.index("\n        }\n")]
        self.assertIn("ApplyLanguage(", cycle)
        apply = shell[shell.index("public void ApplyLanguage(string locale)"):]
        apply = apply[:apply.index("\n        }\n")]
        # 底部狀態列 2026-10-30 拿掉了；常駐的只剩頂欄兩顆徽章與斷線橫幅
        for piece in ("SetConnBadge(_connKey)", "SetRoleBadge(_roleKey)",
                      'NextUi.SetText(_connBannerText, NextStrings.Get("conn_banner"))'):
            self.assertIn(piece, apply)

    def test_every_character_has_a_display_name_per_language(self):
        """角色名分後台名（伺服器 `name`）與顯示名（前台 i18n 的 `ent_{id}`）：每個角色三語都要有顯示名。"""
        import re
        source = (ROOT / "agent" / "brain" / "compose" / "entities.py").read_text(encoding="utf-8")
        ids = re.findall(r'CharacterEntity\(\s*"([a-z0-9_]+)"', source)
        self.assertGreaterEqual(len(ids), 3)
        for locale in gate.LOCALES:
            table = json.loads((I18N / f"next_{locale}.json").read_text(encoding="utf-8"))
            missing = [entity for entity in ids if f"ent_{entity}" not in table]
            self.assertEqual(missing, [], locale)

    def test_screens_show_display_names_not_backend_names(self):
        """畫面上不准直接顯示後台名（`EntityName`）；後台名只在 NextShell 裡存著、換角色時傳給伺服器那一側。"""
        offenders = []
        for path in sorted(NEXT.glob("*.cs")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("//", 1)[0]
                if "EntityName" in code.replace("EntityNameOf", "").replace("EntityDisplayName", "") \
                        and path.name != "NextShell.cs":
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual(offenders, [])

    def test_stream_failures_are_translated_before_display(self):
        """串流斷掉的原文（`audio transport: …`）只進 log，畫面上用界面語言（2026-10-29 無頭送語音抓到）。"""
        chat = (NEXT / "NextChatView.cs").read_text(encoding="utf-8")
        closed = chat[chat.index("private void OnClosed(TurnTicket ticket, string failure)"):]
        closed = closed[:closed.index("\n        }\n")]
        self.assertIn("NextStrings.StreamFailure(failure)", closed)
        self.assertNotIn("SetHint(failure)", closed)
        self.assertNotIn('"reason", failure)', closed)

    def test_views_use_ui_language_for_errors(self):
        api = (NEXT / "NextApi.cs").read_text(encoding="utf-8")
        body = api[api.index("public string UserMessage()"):]
        body = body[:body.index("\n            }\n")]
        self.assertIn("NextStrings.Error(", body)
        self.assertNotIn("return \"", body, "錯誤訊息不能再寫死在 NextApi 裡")


FONTS = ROOT / "app" / "client" / "BlueAgentUnity" / "Assets" / "Resources" / "Fonts"


def font_weight_and_axes(path: pathlib.Path) -> tuple[int, bool]:
    """(OS/2 usWeightClass, 有沒有 fvar)：只讀表目錄，不靠 fontTools。"""
    import struct
    data = path.read_bytes()
    count = struct.unpack(">H", data[4:6])[0]
    tables = {}
    for index in range(count):
        tag, _checksum, offset, _length = struct.unpack(">4sIII", data[12 + 16 * index:28 + 16 * index])
        tables[tag] = offset
    weight = struct.unpack(">H", data[tables[b"OS/2"] + 4:tables[b"OS/2"] + 6])[0]
    return weight, b"fvar" in tables


def font_codepoints(path: pathlib.Path) -> set:
    """字型 cmap 裡有字形的碼位（只讀 format 4／12，夠用；不靠 fontTools）。"""
    import struct
    data = path.read_bytes()
    count = struct.unpack(">H", data[4:6])[0]
    cmap = next(struct.unpack(">I", data[12 + 16 * i + 8:12 + 16 * i + 12])[0]
                for i in range(count) if data[12 + 16 * i:12 + 16 * i + 4] == b"cmap")
    points: set = set()
    tables = struct.unpack(">H", data[cmap + 2:cmap + 4])[0]
    for index in range(tables):
        offset = cmap + struct.unpack(">I", data[cmap + 4 + 8 * index + 4:cmap + 4 + 8 * index + 8])[0]
        kind = struct.unpack(">H", data[offset:offset + 2])[0]
        if kind == 12:
            groups = struct.unpack(">I", data[offset + 12:offset + 16])[0]
            for g in range(groups):
                start, end, _glyph = struct.unpack(">III", data[offset + 16 + 12 * g:offset + 28 + 12 * g])
                points.update(range(start, end + 1))
        elif kind == 4:
            segs = struct.unpack(">H", data[offset + 6:offset + 8])[0] // 2
            ends = struct.unpack(f">{segs}H", data[offset + 14:offset + 14 + 2 * segs])
            starts = struct.unpack(f">{segs}H", data[offset + 16 + 2 * segs:offset + 16 + 4 * segs])
            for start, end in zip(starts, ends):
                if start != 0xFFFF:
                    points.update(range(start, end + 1))
    return points


class TestPerLanguageFonts(unittest.TestCase):
    """界面字按語言換 Noto Sans JP／SC／TC，她的話固定日語字型（2026-10-28）。"""

    def test_every_ui_character_has_a_glyph(self):
        """表裡每個字在該語言的字型裡都有字形（`▸`、`🎲` 這種會變豆腐，2026-10-29 桌面日誌踩到）。"""
        for locale, region in (("ja", "JP"), ("zh-Hans", "SC"), ("zh-Hant", "TC")):
            points = font_codepoints(FONTS / f"NotoSans{region}-Regular.ttf")
            table = json.loads((I18N / f"next_{locale}.json").read_text(encoding="utf-8"))
            missing = sorted({char for text in table.values() for char in text
                              if char != "\n" and ord(char) not in points})
            self.assertEqual(missing, [], f"{locale} 的字 {region} 字型裡沒有")

    def test_glyph_check_catches_tofu(self):
        points = font_codepoints(FONTS / "NotoSansJP-Regular.ttf")
        self.assertIn(ord("あ"), points)
        self.assertNotIn(ord("▸"), points)      # 反向對照：真的缺的那個要抓得到
        self.assertNotIn(0x1F3B2, points)       # 🎲

    def test_fonts_are_static_regular_with_license(self):
        for region in ("JP", "SC", "TC"):
            path = FONTS / f"NotoSans{region}-Regular.ttf"
            self.assertTrue(path.exists(), path.name)
            weight, variable = font_weight_and_axes(path)
            # 可變字型在 Unity 裡畫的是預設實例＝Thin(100)：必須是切好的靜態 Regular
            self.assertEqual((weight, variable), (400, False), path.name)
        license_text = (FONTS / "NotoSansCJK-OFL.txt").read_text(encoding="utf-8")
        self.assertIn("SIL Open Font License", license_text)

    def test_ui_font_follows_language_and_her_text_stays_japanese(self):
        fonts = (NEXT / "NextFonts.cs").read_text(encoding="utf-8")
        for region in ("JP", "SC", "TC"):
            self.assertIn(f"Fonts/NotoSans{region}-Regular", fonts)
        self.assertIn("NextFonts.ApplyUi()", (NEXT / "NextUi.cs").read_text(encoding="utf-8"))
        shell = (NEXT / "NextShell.cs").read_text(encoding="utf-8")
        apply = shell[shell.index("public void ApplyLanguage(string locale)"):]
        self.assertIn("NextFonts.Retint(_topbar)", apply[:apply.index("\n        }\n")])
        chat = (NEXT / "NextChatView.cs").read_text(encoding="utf-8")
        bubble = chat[chat.index("private RectTransform BuildBubble"):]
        bubble = bubble[:bubble.index("\n        }\n")]
        self.assertIn("if (!msg.Mine) NextFonts.UseHer(", bubble)


class TestGateCatchesMistakes(unittest.TestCase):
    """反向對照：每條規則餵一個壞例子。"""

    def test_hardcoded_cjk_is_caught(self):
        bad = 'SetHint("録音中…");'
        self.assertTrue(gate.hardcoded_text({"X.cs": bad}))
        self.assertTrue(gate.hardcoded_text({"X.cs": 'var x = "設定";'}))

    def test_logs_and_comments_are_allowed(self):
        ok = ('// 這是註解「設定」\n'
              'Debug.Log("[next] 履歴 entity=" + e +\n'
              '          "（多行的日誌）");\n'
              'SetHint(NextStrings.Get("settings"));')
        self.assertEqual(gate.hardcoded_text({"X.cs": ok}), [])

    def test_missing_and_unused_keys_are_caught(self):
        table = {"settings": "設定", "orphan": "誰も使わない", "err_offline": "x"}
        source = {"X.cs": 'NextStrings.Get("settings"); NextStrings.F("ghost", "n", 1);'}
        missing, unused = gate.key_usage(source, table)
        self.assertEqual(missing, ["ghost"])
        self.assertEqual(unused, ["orphan"], "err_ 開頭是代碼拼出來的，不算沒人用")

    def test_comparisons_inside_get_are_not_keys(self):
        table = {"game_you_win": "a", "game_draw": "b", "trpg_online": "c", "trpg_stopped": "d"}
        source = {"X.cs": 'NextStrings.Get(winner == "black" ? "game_you_win" : "game_draw");\n'
                          'NextStrings.Get(row.OptBoolean("online") ? "trpg_online" : "trpg_stopped");'}
        missing, unused = gate.key_usage(source, table)
        self.assertEqual((missing, unused), ([], []))

    def test_parity_catches_missing_key_and_placeholder(self):
        tables = {"ja": {"a": "{count} 件", "b": "x"},
                  "zh-Hans": {"a": "条"},
                  "zh-Hant": {"a": "{count} 條", "b": "y"}}
        problems = gate.parity(tables)
        self.assertTrue(any("少了 b" in p for p in problems))
        self.assertTrue(any("a 的佔位符不一致" in p for p in problems))

    def test_duplicate_key_in_json_is_caught(self):
        with tempfile.TemporaryDirectory() as folder:
            base = pathlib.Path(folder)
            for locale in gate.LOCALES:
                (base / f"next_{locale}.json").write_text('{"a": "1"}', encoding="utf-8")
            (base / "next_ja.json").write_text('{"a": "1", "a": "2"}', encoding="utf-8")
            _tables, problems = gate.load_tables(base)
        self.assertTrue(any("重複 key：a" in p for p in problems))


if __name__ == "__main__":
    unittest.main(verbosity=2)
