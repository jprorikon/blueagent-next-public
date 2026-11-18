#!/usr/bin/env python3
"""WP2a 驗收測試：`audit_game_scene_20261115.py` 只讀盤點腳本。

驗證：
  ① 混合庫（確定是桌上的、有歧義的、跨場景取代的、乾淨資料）各類條數、按 entity 分布、turn 對應完全正確；
  ② 盤點前後 SQLite 資料庫與原文檔的 mtime 與 SHA-256 內容雜湊完全不變（嚴格只讀）。
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.adapter.memory_sqlite import SqliteMemory  # noqa: E402
from agent.brain.memory.fact_store import FactStore  # noqa: E402
from agent.brain.memory.facts import fact_key, normalize_text  # noqa: E402
from agent.tools.migrations.audit_game_scene_20261115 import (  # noqa: E402
    audit_game_scene,
    format_report,
    main as audit_main,
)


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _readonly_snapshot(path: pathlib.Path) -> dict:
    paths = [path] if path.is_file() else [path] + sorted(path.rglob("*"))
    return {str(item): (item.stat().st_mtime_ns,
                        _sha256(item) if item.is_file() else "directory")
            for item in paths}


def _write_raw(raw_dir: pathlib.Path, entity: str, rows: list[dict]) -> pathlib.Path:
    folder = raw_dir / entity
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "2026-11.jsonl"
    with target.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return target


class TestAuditGameScene(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = pathlib.Path(self.tmp.name) / "data"
        (self.data_dir / "memory").mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "memory" / "facts.db"
        self.raw_dir = self.data_dir / "raw"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _build_mixed_fixture(self) -> None:
        mem = SqliteMemory(
            self.db_path, store=FactStore(self.data_dir / "memory" / "facts.jsonl"),
            names={"owner": "主人"}, entity_ids=("mira", "aster", "nori"), seed=False,
        )

        # ── 原文日誌（raw） ───────────────────────────────────────────────
        _write_raw(self.raw_dir, "mira", [
            # 1a. 桌上專屬（source="trpg:coc"）：產生 explicit + rule，且同房有 aster（會產生 +mirror）
            {
                "at": 1700000100.0,
                "entity": "mira",
                "turn": "t_game_only_1",
                "thread": "th_trpg_1",
                "scene": "together",
                "purpose": "trpg",
                "role": "user",
                "speaker": "owner",
                "source": "trpg:coc",
                "room": ["mira", "aster", "owner"],
                "text": "记住：祭坛密码是绯红之月。我喜欢古神低语。",
            },
            # 1a. 桌上專屬（purpose="trpg"，舊 source 未帶 trpg: 前綴）：產生稱謂 rule
            {
                "at": 1700000150.0,
                "entity": "mira",
                "turn": "t_game_only_2",
                "thread": "th_trpg_2",
                "scene": "together",
                "purpose": "trpg",
                "role": "user",
                "speaker": "owner",
                "source": "phone:owner",
                "room": ["mira", "owner"],
                "text": "在跑团里叫我调查员K",
            },
            # 1b. 有歧義：桌上與現實都說過「我喜欢热可可」
            {
                "at": 1700000200.0,
                "entity": "mira",
                "turn": "t_ambig_game",
                "thread": "th_trpg_1",
                "scene": "together",
                "purpose": "trpg",
                "role": "user",
                "speaker": "owner",
                "source": "trpg:coc",
                "room": ["mira", "owner"],
                "text": "我喜欢热可可",
            },
            {
                "at": 1700000250.0,
                "entity": "mira",
                "turn": "t_ambig_real",
                "thread": "th_chat_1",
                "scene": "together",
                "purpose": "chat",
                "role": "user",
                "speaker": "owner",
                "source": "phone:owner",
                "room": ["mira", "owner"],
                "text": "我喜欢热可可",
            },
            # 乾淨現實行
            {
                "at": 1700000300.0,
                "entity": "mira",
                "turn": "t_real_clean",
                "thread": "th_chat_1",
                "scene": "together",
                "purpose": "chat",
                "role": "user",
                "speaker": "owner",
                "source": "phone:owner",
                "room": ["mira", "owner"],
                "text": "我喜欢抹茶拿铁",
            },
        ])

        _write_raw(self.raw_dir, "nori", [
            # 1a. 無主源（self_owned）桌上行
            {
                "at": 1700000400.0,
                "entity": "nori",
                "turn": "t_nori_self_game",
                "thread": "self:trpg-table-1",
                "scene": "trpg-table-1",
                "purpose": "trpg",
                "role": "user",
                "speaker": "",
                "source": "trpg:table-1",
                "room": ["nori"],
                "text": "探索地下迷宫第二层",
            },
        ])

        # ── 寫入 SQLite 模擬歷史髒資料與乾淨資料 ──────────────────────────
        # [髒 1a - 確定是桌上的] 舊版未打 game=1，落成了 game=0（共 5 條：mira 3 條、aster mirror 1 條、nori self 1 條）
        mem.add("主人说过：记住：祭坛密码是绯红之月。我喜欢古神低语。",
                layer="circle", entity="mira", source="explicit",
                created_at=1700000101.0, game=0)
        mem.add("主人 喜欢古神低语",
                layer="circle", entity="mira", source="rule",
                created_at=1700000102.0, game=0)
        mem.add("主人 喜欢古神低语",
                layer="circle", entity="aster", source="rule+mirror",
                created_at=1700000103.0, game=0)
        mem.add("主人希望被叫「调查员K」",
                layer="pair", entity="mira", source="rule",
                created_at=1700000151.0, game=0)
        mem.add("她在「trpg-table-1」：探索地下迷宫第二层",
                layer="self", entity="nori", source="self", self_owned=True,
                created_at=1700000401.0, game=0)

        # [1b - 有歧義] 桌上與現實都講過，留着 game=0 不動（1 條）
        mem.add("主人 喜欢热可可",
                layer="pair", entity="mira", source="rule",
                created_at=1700000201.0, game=0)

        # [乾淨資料]
        # · 現實規則事實（game=0）
        mem.add("主人 喜欢抹茶拿铁",
                layer="pair", entity="mira", source="rule",
                created_at=1700000301.0, game=0)
        # · 已正確標為 game=1 的桌上規則事實
        mem.add("主人说过：记住：祭坛密码是绯红之月。我喜欢古神低语。",
                layer="circle", entity="nori", source="explicit",
                created_at=1700000105.0, game=1)
        # · 非規則來源的現實記憶（distill）
        mem.add("主人最近在整理书架",
                layer="pair", entity="mira", source="distill",
                created_at=1700000350.0, game=0)

        # [髒 2 - 跨場景取代]
        # 舊版無 game 過濾時造成的跨場景取代（直接以 SQL 模擬歷史庫中已存在的跨場景 superseded_by）
        real_hp = mem.add("主人目前精力充沛（现实状态）",
                          layer="pair", entity="mira", source="distill",
                          kind="state", slot="vitality",
                          created_at=1700000500.0, game=0)
        game_hp = mem.add("调查员HP只剩1点（桌上状态）",
                          layer="pair", entity="mira", source="distill",
                          kind="state", slot="vitality",
                          created_at=1700000510.0, game=1)

        game_loc = mem.add("アスター在旧校舍二楼调查（桌上位置）",
                           layer="pair", entity="aster", source="distill",
                           kind="state", slot="location",
                           created_at=1700000600.0, game=1)
        real_loc = mem.add("アスター在客厅看书（现实位置）",
                           layer="pair", entity="aster", source="distill",
                           kind="state", slot="location",
                           created_at=1700000610.0, game=0)

        # [乾淨同場景取代] game=0 -> game=0
        old_drink = mem.add("主人今天喝冰美式",
                            layer="pair", entity="mira", source="distill",
                            kind="state", slot="today_drink",
                            created_at=1700000700.0, game=0)
        new_drink = mem.add("主人今天改喝热拿铁",
                            layer="pair", entity="mira", source="distill",
                            kind="state", slot="today_drink",
                            created_at=1700000710.0, game=0)

        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE facts SET superseded_by = ?, valid_to = ? WHERE entity = ? AND key = ?",
                    (fact_key(game_hp.text), 1700000510.0, "mira", fact_key(real_hp.text)),
                )
                conn.execute(
                    "UPDATE facts SET superseded_by = ?, valid_to = ? WHERE entity = ? AND key = ?",
                    (fact_key(real_loc.text), 1700000610.0, "aster", fact_key(game_loc.text)),
                )
                conn.execute(
                    "UPDATE facts SET superseded_by = ?, valid_to = ? WHERE entity = ? AND key = ?",
                    (fact_key(new_drink.text), 1700000710.0, "mira", fact_key(old_drink.text)),
                )
        finally:
            conn.close()

    def test_audit_counts_categories_and_preserves_db_mtime_and_hash(self) -> None:
        self._build_mixed_fixture()

        # 將 mtime 設為過去固定值，確保任何寫入或觸碰都會暴露
        fixed_mtime = 1700000000
        os.utime(self.db_path, (fixed_mtime, fixed_mtime))
        before_stat = self.db_path.stat()
        before_hash = _sha256(self.db_path)
        tracked = [self.db_path, self.data_dir / "memory" / "facts.jsonl", self.raw_dir]
        before_readonly = {path: _readonly_snapshot(path) for path in tracked}

        report = audit_game_scene(self.data_dir)

        after_stat = self.db_path.stat()
        after_hash = _sha256(self.db_path)
        self.assertEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)
        self.assertEqual(before_hash, after_hash)
        self.assertEqual({path: _readonly_snapshot(path) for path in tracked}, before_readonly)
        self.assertEqual(report["unexplained_rule_facts"]["count"], 0)
        self.assertEqual(report["raw_coverage"]["files"], 2)
        self.assertEqual(report["raw_coverage"]["lines"], 6)
        self.assertEqual(report["raw_coverage"]["game_lines"], 4)
        self.assertEqual(report["raw_coverage"]["real_lines"], 2)
        self.assertEqual(report["raw_coverage"]["earliest_at"], 1700000100.0)
        self.assertEqual(report["raw_coverage"]["latest_at"], 1700000400.0)

        # 第 1a 類：確定是桌上的（共 5 條：mira=3, aster=1, nori=1）
        definite = report["definite_table_as_real"]
        self.assertEqual(definite["count"], 5)
        self.assertEqual(definite["by_entity"], {"nori": 1, "mira": 3, "aster": 1})
        definite_texts = {(item["entity"], item["text"], item["source"], item["turn"])
                          for item in definite["items"]}
        self.assertEqual(definite_texts, {
            ("mira", "主人说过：记住：祭坛密码是绯红之月。我喜欢古神低语。", "explicit", "t_game_only_1"),
            ("mira", "主人 喜欢古神低语", "rule", "t_game_only_1"),
            ("aster", "主人 喜欢古神低语", "rule+mirror", "t_game_only_1"),
            ("mira", "主人希望被叫「调查员K」", "rule", "t_game_only_2"),
            ("nori", "她在「trpg-table-1」：探索地下迷宫第二层", "self", "t_nori_self_game"),
        })

        # 第 1b 類：有歧義（共 1 條：mira=1，同時命中桌上與現實 turn）
        ambig = report["ambiguous_table_as_real"]
        self.assertEqual(ambig["count"], 1)
        self.assertEqual(ambig["by_entity"], {"mira": 1})
        self.assertEqual(ambig["samples"][0]["text"], "主人 喜欢热可可")
        self.assertEqual(ambig["samples"][0]["turn"], "t_ambig_game")
        self.assertEqual(ambig["samples"][0]["real_turn"], "t_ambig_real")

        # 第 2 類：跨場景取代（共 2 條：mira=1 (game 0->1), aster=1 (game 1->0)；同場景的不算）
        cross = report["cross_scene_supersede"]
        self.assertEqual(cross["count"], 2)
        self.assertEqual(cross["by_entity"], {"mira": 1, "aster": 1})
        cross_pairs = {(item["entity"], item["game"], item["target_game"], item["text"])
                       for item in cross["items"]}
        self.assertEqual(cross_pairs, {
            ("mira", 0, 1, "主人目前精力充沛（现实状态）"),
            ("aster", 1, 0, "アスター在旧校舍二楼调查（桌上位置）"),
        })

        # CLI --out 驗證，且不碰資料目錄
        out_file = pathlib.Path(self.tmp.name) / "audit_report.txt"
        rc = audit_main(["--data", str(self.data_dir), "--copy-check", "--out", str(out_file)])
        self.assertEqual(rc, 0)
        rendered = out_file.read_text(encoding="utf-8")
        self.assertIn("1a. 被寫成現實的桌上事實（確定是桌上的）", rendered)
        self.assertIn("1u. 無法由現存原文解釋的規則事實", rendered)
        self.assertIn("t_game_only_1", rendered)
        self.assertEqual(before_stat.st_mtime_ns, self.db_path.stat().st_mtime_ns)
        self.assertEqual(before_hash, _sha256(self.db_path))
        self.assertEqual({path: _readonly_snapshot(path) for path in tracked}, before_readonly)

    def test_audit_jsonl_fallback_when_sqlite_db_absent(self) -> None:
        from agent.brain.memory.fact_store import FactStore
        jsonl_path = self.data_dir / "memory" / "facts.jsonl"
        store = FactStore(jsonl_path)
        mem = SqliteMemory(
            self.db_path, store=store, names={"owner": "主人"},
            entity_ids=("mira",), seed=False,
        )
        _write_raw(self.raw_dir, "mira", [{
            "at": 1700000100.0,
            "entity": "mira",
            "turn": "t_game_jsonl",
            "thread": "th_trpg",
            "scene": "together",
            "purpose": "trpg",
            "role": "user",
            "speaker": "owner",
            "source": "trpg:coc",
            "room": ["mira", "owner"],
            "text": "我喜欢深海章鱼",
        }])
        mem.add("主人 喜欢深海章鱼", layer="pair", entity="mira",
                source="rule", created_at=1700000101.0, game=0)
        store.append_op("revive", key=fact_key("不存在的取代者"))
        # 移除 facts.db，模擬本地只有正本 facts.jsonl 的情況
        self.db_path.unlink()
        tracked = [jsonl_path, self.raw_dir]
        before = {path: _readonly_snapshot(path) for path in tracked}

        report = audit_game_scene(self.data_dir)
        self.assertFalse(self.db_path.exists())
        self.assertEqual({path: _readonly_snapshot(path) for path in tracked}, before)
        self.assertEqual(report["definite_table_as_real"]["count"], 1)
        self.assertEqual(report["total_facts"], 1, "追加操作记录不能算成事实")
        self.assertEqual(report["definite_table_as_real"]["samples"][0]["turn"], "t_game_jsonl")

    def test_changed_display_name_is_unexplained_not_dirty(self) -> None:
        mem = SqliteMemory(
            self.db_path, store=FactStore(self.data_dir / "memory" / "facts.jsonl"),
            names={"owner": "主人"}, seed=False)
        _write_raw(self.raw_dir, "mira", [{
            "at": 1700000100.0, "entity": "mira", "turn": "t_new_name",
            "role": "user", "speaker": "owner", "source": "phone:owner",
            "purpose": "chat", "room": ["mira", "owner"], "text": "我喜欢热可可",
        }])
        mem.add("旧主人 喜欢热可可", layer="pair", entity="mira",
                source="rule", created_at=1700000101.0, game=0)
        mem.close()
        report = audit_game_scene(self.data_dir, now=1763203200.0)
        self.assertEqual(report["definite_table_as_real"]["count"], 0)
        self.assertEqual(report["ambiguous_table_as_real"]["count"], 0)
        unexplained = report["unexplained_rule_facts"]
        self.assertEqual(unexplained["count"], 1)
        self.assertEqual(unexplained["by_entity"], {"mira": 1})
        self.assertEqual(unexplained["samples"][0]["text"], "旧主人 喜欢热可可")
        self.assertEqual(report["raw_coverage"]["keep_months"], 12)
        rendered = format_report(report)
        self.assertIn("1a=0 不能解讀成「庫是乾淨的」", rendered)
        self.assertIn("RawLog 保留期：12 個月", rendered)

    def test_cli_requires_copy_confirmation(self) -> None:
        self._build_mixed_fixture()
        with self.assertRaises(SystemExit) as caught:
            audit_main(["--data", str(self.data_dir)])
        self.assertEqual(caught.exception.code, 2)

    def test_ten_thousand_facts_and_raw_lines(self) -> None:
        mem = SqliteMemory(self.db_path, seed=False)
        rows, raw_rows = [], []
        for index in range(10_000):
            text = f"主人 喜欢合成饮品{index}"
            rows.append((text, normalize_text(text), "pair", "owner", "mira", "rule",
                         1700000000.0 + index, fact_key(text)))
            raw_rows.append({
                "at": 1700000000.0 + index, "entity": "mira", "turn": f"t_{index}",
                "role": "user", "speaker": "owner", "source": "phone:owner",
                "purpose": "chat", "room": ["mira", "owner"],
                "text": f"我喜欢合成饮品{index}",
            })
        with mem._db:
            mem._db.executemany(
                "INSERT INTO facts (text, norm, layer, principal, entity, source, created_at, key) "
                "VALUES (?,?,?,?,?,?,?,?)", rows)
        mem.close()
        _write_raw(self.raw_dir, "mira", raw_rows)

        report = audit_game_scene(self.data_dir)
        self.assertEqual(report["total_facts"], 10_000)
        self.assertEqual(report["raw_coverage"]["lines"], 10_000)
        self.assertEqual(report["raw_coverage"]["real_lines"], 10_000)
        self.assertEqual(report["definite_table_as_real"]["count"], 0)
        self.assertEqual(report["unexplained_rule_facts"]["count"], 0)
        self.assertGreater(report["performance"]["elapsed_s"], 0.0)
        self.assertGreater(report["performance"]["process_peak_rss_bytes"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
