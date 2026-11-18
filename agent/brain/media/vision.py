"""看圖：**圖 → 本地 VL → 一段文字 → 腦**（照舊橋 `ba/media/vision.py` 的做法搬過來）。

為什麼不是把圖丟給腦：現役腦（DeepSeek）不吃圖（舊橋實測 `content.type is invalid`），
所以要一台本地 VL 先讀成文字，圖本身不進對話上下文——換腦時這一層不用動。

⚠️ 那台 VL（`:11435`、qwen38-27b）是**出圖質檢專用、單 worker 串行**（~10 s/枚）：
看一眼可以，別當高頻。失敗一律回空字串（呼叫端把「讀不到」當事實寫進 prompt，
不要讓一整輪對話因為 VL 掛掉而死）。
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request

VISION_URL = os.environ.get("VISION_URL", "http://127.0.0.1:11435/v1/chat/completions").strip()
VISION_MODEL = os.environ.get("VISION_MODEL", "qwen38-27b").strip()
VISION_TIMEOUT_S = float(os.environ.get("VISION_TIMEOUT_S", "90") or "90")
#: owner 2026-10-23：看圖也走 DeepSeek（`deepseek-flash` 吃圖）；金鑰沿用大腦那把。
VISION_KEY = (os.environ.get("VISION_API_KEY") or os.environ.get("BLUEAGENT_LLM_API_KEY")
              or os.environ.get("DEEPSEEK_API_KEY") or "").strip()
VISION_THINKING = os.environ.get("VISION_THINKING", "off").strip().lower() in {"1", "on", "true", "yes"}

#: `deepseek-flash` 這種會先「思考」的模型：`max_tokens` 太小時思考把額度吃光、
#: `content` 回空字串（實測 2026-10-27：400 → content="" ／ reasoning 300+ 字，
#: 於是進 prompt 的是「読めませんでした」，看起來就像她看不到圖）。
#: 思考關不掉時**至少**要有這麼多額度，答案才擠得出來；`max_tokens` 是上限不是花費。
VISION_MIN_MAX_TOKENS = 1200

INSTRUCTIONS = (
    "これはオーナー（ユーザー）が送ってきた画像です。"
    "あなたは AI の彼女に『いま何が写っているか』を伝える係です。日本語で、事実だけを、次の形で：\n"
    "1) 何の画像か（人物／風景／スクリーンショット／食べ物／物…）\n"
    "2) 読める文字はそのまま（自信がなければ『たぶん…』と書く）\n"
    "3) 目立つ要素（色・表情・小物・構図）\n"
    "4) 全体で 6 行以内。**推測で埋めない**——読めないものは『読めない』と書く。"
)


def _role() -> dict:
    """`vision` 角色實際生效的設定（`data/llm.json` → env → 預設）。"""
    try:
        from agent.brain.config import llm as llm_config
        return llm_config.settings_for("vision")
    except Exception:                                   # noqa: BLE001
        return {"model": VISION_MODEL, "base_url": VISION_URL, "max_tokens": 400,
                "temperature": 0.2, "timeout_s": VISION_TIMEOUT_S,
                "api_key": VISION_KEY}


def _endpoint(base_url: str) -> str:
    base = str(base_url or "").rstrip("/")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def describe(image: bytes, extra: str = "", mime: str = "image/png") -> str:
    """圖片 → 日文短描述。任何失敗都回 `""`（呼叫端負責說「讀不到」）。"""
    if not image:
        return ""
    prompt = INSTRUCTIONS + (f"\n\nオーナーの添えた言葉：{extra}" if extra else "")
    role = _role()
    payload = {
        "model": role.get("model") or VISION_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {
                "url": f"data:{mime};base64," + base64.b64encode(image).decode("ascii")}},
        ]}],
        "max_tokens": int(role.get("max_tokens") or 400),
        "temperature": float(role.get("temperature") or 0.2),
    }
    payload["max_tokens"] = max(VISION_MIN_MAX_TOKENS, int(payload["max_tokens"]))
    if not VISION_THINKING:
        from agent.brain.config import llm as _llm
        payload.update(_llm.thinking_payload(enable=False,
                                             base_url=str(role.get("base_url") or VISION_URL)))
    key = str(role.get("api_key") or VISION_KEY or "").strip()
    base_url = str(role.get("base_url") or VISION_URL)
    from agent.brain.config import llm as llm_config
    if not llm_config.usable(base_url, key):
        # 遠端沒 key：不打一支注定 401 的呼叫（但要留痕，否則跟「VL 讀不出」分不出來）
        print(f"[vision] 跳過：base_url={base_url} 沒有可用 key", flush=True)
        return ""
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(
        _endpoint(base_url),
        data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(
                request, timeout=float(role.get("timeout_s") or VISION_TIMEOUT_S)) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = str(message.get("content") or "").strip()
        if not text:
            # 靜默失敗是上一次「她說看不到圖」查不出來的原因 ⇒ 一律留一行痕跡
            print(f"[vision] content 空（finish={choice.get('finish_reason')} "
                  f"reasoning={len(str(message.get('reasoning_content') or ''))}字 "
                  f"max_tokens={payload['max_tokens']}）", flush=True)
        return text
    except Exception as exc:                            # noqa: BLE001 - 看圖失敗不該殺這一輪
        print(f"[vision] 呼叫失敗（{type(exc).__name__}: {exc}）", flush=True)
        return ""
