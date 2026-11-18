#!/usr/bin/env python3
"""用**真的 prompt**量前綴快取：現在這一版 vs 舊排法（每回合變的東西放 system 頭）。

為什麼要有這支（owner 2026-11-01 拍板「①改了試試」）：ADR-0020 的整套排法唯一能驗的
數字就是「命中率／未命中 token」。它壞掉的時候回覆照樣正常，只是帳單變貴、延遲變高
——所以要有可重跑的實測，不能靠感覺。

    python3 agent/tools/cache_probe.py --turns 6                # 只量現在這一版
    python3 agent/tools/cache_probe.py --turns 6 --legacy       # 兩版並排（各跑一輪，成本很小）

金鑰只從檔案讀（`--env` 指定，預設 `.secrets/deepseek.env`），不進命令列。
同一句歷史、同一段人設、同樣的動態內容（記憶／時間感）——兩版只差在**排法**。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import string
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent.brain.messages import build_messages  # noqa: E402
from agent.brain.ports import BrainRequest  # noqa: E402

#: 舊排法要搬回前面的那幾塊（每回合都變的東西）
TURN_MARKERS = ("[[memory]]", "[[clock]]", "[[before]]", "[[utterance]]", "[[notes]]",
                "[[motion]]", "[[call]]")
HISTORY_HEAD = ("u", "a")


def read_key(path: str, name: str = "DEEPSEEK_API_KEY") -> tuple[str, str, str]:
    key, base, model = "", "https://api.deepseek.com/v1", "deepseek-flash"
    try:
        for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
            if line.startswith(name + "="):
                key = line.split("=", 1)[1].strip()
            elif line.startswith("DEEPSEEK_BASE="):
                base = line.split("=", 1)[1].strip()
            elif line.startswith("DEEPSEEK_MODEL="):
                model = line.split("=", 1)[1].strip()
    except OSError:
        pass
    return key, base, model


def legacy_layout(messages: list[dict]) -> list[dict]:
    """把「本輪段」搬回 system 頭（＝2026-11-01 之前的排法），其他一律不動。"""
    head, moved, rest = [], [], []
    for index, row in enumerate(messages):
        is_turn = any(marker in str(row.get("content") or "") for marker in TURN_MARKERS)
        if index == 0:
            head.append(row)
        elif is_turn:
            moved.append(row)
        else:
            rest.append(row)
    return head + moved + rest


def call(base: str, key: str, model: str, messages: list[dict]) -> dict | None:
    body = json.dumps({"model": model, "messages": messages, "max_tokens": 80,
                       "stream": False}).encode()
    request = urllib.request.Request(
        base + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.load(response)
    except Exception as exc:                                  # noqa: BLE001
        print(f"  ✗ {type(exc).__name__}: {exc}", flush=True)
        return None


def transcript(turns: int) -> list[dict]:
    """跟她真實的往來一個量級：她回 60–120 字、主人說 20–50 字。"""
    rows: list[dict] = []
    for index in range(turns):
        # 主人說 30–50 字、她回 100–140 字（跟現役的往來一個量級）
        rows.append({"role": "user",
                     "text": f"u{index:03d} " + "今日はこんな話があってね、ちょっと聞いてほしいんだ。" * 2})
        rows.append({"role": "assistant",
                     "text": f"a{index:03d} " + "うん、そうなんだ。それは大変だったね、もう少し詳しく聞かせて。" * 6})
    return rows


def request_for(turns: int, *, persona: str) -> BrainRequest:
    return BrainRequest(
        thread_id="th_probe", entity="mira", principal="owner",
        text="じゃあ、その続きを話すね", system=persona,
        meta={"transcript": transcript(turns), "speak_lang": "ja",
              "recall": ["主人は『佐々木とピーちゃん』を六巻まで読んでいる",
                         "主人は甘いものが苦手だったが最近は食べられる",
                         "今夜いっしょにキャラシートを作る約束をした"],
              "clock_block": f"いま 12:0{turns}、{turns * 2 + 1} 往復目、前の発言は 3 分前",
              "carry_summary": "前に小説と TRPG の話をした"})


def run(label: str, *, turns: int, base: str, key: str, model: str,
        persona: str, legacy: bool) -> dict:
    # 每一輪都換一個鹽：兩版**不能共用同一段快取**，否則第二版會白吃第一版留下的命中
    salt = "".join(random.choices(string.ascii_lowercase, k=8))
    persona = persona + "（計測用識別 " + salt + "）"
    print(f"── {label}（{turns} 回合，識別 {salt}）", flush=True)
    hits = misses = 0
    for turn in range(1, turns + 1):
        messages = build_messages(request_for(turn, persona=persona))
        if legacy:
            messages = legacy_layout(messages)
        reply = call(base, key, model, messages)
        if reply is None:
            return {}
        usage = reply.get("usage") or {}
        prompt = int(usage.get("prompt_tokens") or 0)
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        miss = int(usage.get("prompt_cache_miss_tokens") or (prompt - hit))
        hits, misses = hits + hit, misses + miss
        rate = (100.0 * hit / prompt) if prompt else 0.0
        print(f"  turn{turn}: prompt={prompt} hit={hit} miss={miss} 命中={rate:.1f}%",
              flush=True)
        time.sleep(1.0)
    total = hits + misses
    print(f"  合計 hit={hits} miss={misses} 命中率={100.0 * hits / total if total else 0:.1f}%")
    return {"hit": hits, "miss": misses}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=6)
    parser.add_argument("--env", default=str(ROOT / ".secrets" / "deepseek.env"))
    parser.add_argument("--legacy", action="store_true", help="並排跑舊排法")
    args = parser.parse_args(argv)

    key, base, model = read_key(args.env)
    if not key:
        print(f"✗ {args.env} 裡沒有 DEEPSEEK_API_KEY", file=sys.stderr)
        return 2
    persona_path = ROOT / "agent" / "persona" / "SOUL.md"
    persona = persona_path.read_text(encoding="utf-8") if persona_path.exists() else "persona"
    print(f"模型 {model}｜人設 {len(persona)} 字｜金鑰來自 {args.env}")

    now = run("現在（本輪段在歷史之後）", turns=args.turns, base=base, key=key,
              model=model, persona=persona, legacy=False)
    old = {}
    if args.legacy:
        old = run("舊排法（本輪段在 system 頭）", turns=args.turns, base=base, key=key,
                  model=model, persona=persona, legacy=True)
    if now and old:
        saved = 100.0 * (1 - now["miss"] / max(1, old["miss"]))
        print(f"\n未命中 token：舊 {old['miss']} → 現 {now['miss']}（少 {saved:.1f}%）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
