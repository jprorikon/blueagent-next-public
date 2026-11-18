"""人格編譯（SPIN 第 ① 段的縮小版）：把散在 prose 裡的人格，壓成一張**結構化 core**。

為什麼要這個（owner 2026-10-22 給了 arXiv 2610.08328）：
  我們現在每輪都把「人格檔全文 ＋ 情境 ＋ transcript」一起餵進去 —— 論文稱之為 static persona
  prompting，問題是**把穩定傾向和情境解讀纏在一起**：同一個她，被催和被誇時的反應不成體系。
  SPIN 的做法是拆三段：① 編譯出 task-blind 的人格 core（每人一次、快取）② 用 core × 當前情境
  引出 transient state ③ 只看 state 做決定。

這一支只做第 ① 段，而且**不增加每輪延遲**：
  · 編譯一次，存 `data/persona/<entity>.core.json`（帶 soul 的 hash，改了人格檔才重編譯）；
  · prompt 只放「2–3 句人格肖像 ＋ 最強 2–3 條 if-then 傾向」——不放 JSON，免得她講話變成表格腔；
  · 編譯失敗／沒模型 ⇒ 回 None，整條路一行都不多（可回退）。

維度：SPIN 的五個（公平／規範／互惠／不確定／自保）＋ 我們這個用途需要的兩個
（黏人 clinginess、表達欲 expressiveness）。理由：論文那五個是社會困境維度，
「陪伴」真正在意的是「她多需要你、多願意講」。
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import urllib.request

#: SPIN 的五個維度 ＋ 陪伴需要的兩個。
TRAIT_DIMENSIONS: tuple[str, ...] = (
    "fairness_sensitivity", "norm_sensitivity", "reciprocity_orientation",
    "uncertainty_aversion", "self_protection_tendency",
    "clinginess", "expressiveness",
)

#: 允許的 trigger 標籤（SPIN 的七個 ＋ 陪伴場景的四個）。
TRIGGERS: tuple[str, ...] = (
    "fairness_violation", "uncertainty", "reciprocity_signal", "authority_pressure",
    "social_norm", "betrayal_risk", "competitive_opponent",
    "being_ignored", "praise", "command", "teasing",
)

PROMPT = """あなたは人格アセスメントの専門家です。以下の人物プロフィールから、
**課題に依存しない安定した人格コア**を推定してください。

ルール：
- 与えられたプロフィールだけから推定する（課題・場面・実験を想像しない）。
- 7 つの次元を必ず出す（0.0–1.0 の実数）。
- 傾向ルールは 2〜5 条。trigger は次の中から選ぶ：{triggers}
- 出力は厳密な JSON のみ（前後に説明を書かない）。

スキーマ：
{{"trait_dimensions": {{{dims}}},
 "disposition_rules": [{{"trigger": "<label>", "tendency": "<短い説明>", "strength": 0.0}}],
 "default_decision_style": "<短いラベル>",
 "portrait": "<この人がどんな人か、2〜3 文。日本語>"}}

人物プロフィール：
{profile}
"""


def _soul_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:12]


def core_path(root, entity: str) -> pathlib.Path:
    return pathlib.Path(root) / f"{entity}.core.json"


def load_core(root, entity: str, *, soul_text: str = "") -> dict | None:
    """讀已有的 core；人格檔變過（hash 不符）就當沒有。"""
    path = core_path(root, entity)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("trait_dimensions"):
        return None
    if soul_text and data.get("soul_hash") and data["soul_hash"] != _soul_hash(soul_text):
        return None
    return data


def save_core(root, entity: str, core: dict) -> pathlib.Path | None:
    path = core_path(root, entity)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(core, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return None
    return path


def parse_core(raw: str, *, soul_text: str = "") -> dict | None:
    """把模型輸出解析成 core；形狀不對就回 None（寧可沒有 core，也不要半個）。"""
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    dims = data.get("trait_dimensions")
    if not isinstance(dims, dict):
        return None
    clean: dict[str, float] = {}
    for name in TRAIT_DIMENSIONS:
        try:
            clean[name] = round(min(1.0, max(0.0, float(dims.get(name)))), 3)
        except (TypeError, ValueError):
            return None
    rules = []
    for row in (data.get("disposition_rules") or []):
        if not isinstance(row, dict):
            continue
        trigger = str(row.get("trigger") or "").strip()
        tendency = str(row.get("tendency") or "").strip()
        if trigger not in TRIGGERS or not tendency:
            continue
        try:
            strength = round(min(1.0, max(0.0, float(row.get("strength")))), 3)
        except (TypeError, ValueError):
            strength = 0.5
        rules.append({"trigger": trigger, "tendency": tendency[:120], "strength": strength})
    if not rules:
        return None
    rules.sort(key=lambda item: -float(item.get("strength") or 0.0))
    return {
        "trait_dimensions": clean,
        "disposition_rules": rules[:5],
        "default_decision_style": str(data.get("default_decision_style") or "")[:60],
        "portrait": str(data.get("portrait") or "")[:400],
        "soul_hash": _soul_hash(soul_text),
        "compiled_at": __import__("time").time(),
    }


def core_hash(core: dict | None) -> str:
    if not core:
        return ""
    body = {key: core.get(key) for key in ("trait_dimensions", "disposition_rules", "portrait")}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True)
                          .encode("utf-8")).hexdigest()[:12]


def prompt_block(core: dict | None, *, max_rules: int = 3, max_chars: int = 700) -> str:
    """給 prompt 的那一小段：**人格肖像 ＋ 最強的幾條 if-then**，不放 JSON。

    owner 2026-10-22 的顧慮：把 core 原樣塞進去她就會用問卷腔說話。所以只放她在意的東西：
    「她是誰」＋「什麼情況她會怎麼反應」。
    """
    if not core:
        return ""
    portrait = str(core.get("portrait") or "").strip()
    rules = [row for row in (core.get("disposition_rules") or [])
             if isinstance(row, dict) and row.get("tendency")][:max(0, int(max_rules))]
    lines: list[str] = []
    if portrait:
        lines.append(portrait)
    for row in rules:
        trigger = str(row.get("trigger") or "").replace("_", " ")
        lines.append(f"・{trigger} のとき：{row['tendency']}")
    if not lines:
        return ""
    body = "\n".join(lines)[:max_chars]
    return "### あなたの核（変わらない部分）\n" + body


def compile_core(profile: str, *, soul_text: str = "", call=None,
                 timeout: float = 40.0, log=None) -> dict | None:
    """跑一次編譯（`call(prompt) -> str` 由呼叫端給，方便測試與換模型）。"""
    say = log or (lambda _message: None)
    prompt = PROMPT.format(
        triggers=", ".join(TRIGGERS),
        dims=", ".join(f'"{name}": 0.0' for name in TRAIT_DIMENSIONS),
        profile=str(profile or "").strip()[:4000],
    )
    if call is None:
        return None
    try:
        raw = call(prompt)
    except Exception as exc:                            # noqa: BLE001 - 編譯失敗不擋對話
        say(f"persona-core: 編譯失敗（{type(exc).__name__}: {exc}）")
        return None
    core = parse_core(raw, soul_text=soul_text)
    if core is None:
        say(f"persona-core: 輸出形狀不對（{str(raw)[:80]!r}）")
    return core


def http_call(env_values, *, log=None, max_tokens: int = 2400,
              disable_thinking: bool = False, role: str = ""):
    """預設的編譯通道：用跟大腦同一組 `BLUEAGENT_LLM_*`（不另立第二套設定）。

    遠端端點沒有 key ⇒ 回 `None`（＝沒有這個通道），呼叫端各自跳過；不要建一支注定 401 的呼叫。

    `role`（owner 2026-11-02）：走 `config/llm.settings_for()` 那張角色表，**跟大腦分開**。
    心情定位／內心獨白這種每跳都會叫的小活，以前是直接用主腦那組（同一台 125B、同一條
    推論 slot）⇒ 主人一句話進來時跟他搶隊列。給它們一個窄模型的 role，主腦那條就空出來了。
    `role=""` 維持舊行為（誰呼叫都吃 `BLUEAGENT_LLM_*`）。
    """
    # 沿用大腦那一組設定（別另立第二套）：BLUEAGENT_LLM_* 優先，其次 DEEPSEEK_*
    #（容器現在走 BLUEAGENT_BRAIN=deepseek，env 名就是 DEEPSEEK_*——2026-10-22 實測踩到）
    base = str(env_values.get("BLUEAGENT_LLM_BASE_URL") or env_values.get("DEEPSEEK_BASE")
               or "https://api.deepseek.com/v1")
    model = str(env_values.get("BLUEAGENT_LLM_MODEL") or env_values.get("DEEPSEEK_MODEL")
                or "deepseek-chat")
    key = str(env_values.get("BLUEAGENT_LLM_API_KEY") or env_values.get("DEEPSEEK_API_KEY") or "")
    if role:
        from agent.brain.config import llm as _roles
        settings = _roles.settings_for(str(role), env=env_values)
        base = str(settings.get("base_url") or base)
        model = str(settings.get("model") or model)
        key = str(settings.get("api_key") or key)
    url = base.rstrip("/") + "/chat/completions"
    from agent.brain.config import llm as llm_config
    if not llm_config.usable(base, key):
        if log is not None:
            log(f"llm: {base} 沒有 key，這個通道不建")
        return None

    def call(prompt: str) -> str:
        # 思考型模型（DeepSeek）會把 token 花在 reasoning 上 ⇒ content 空、JSON 拿不到
        # （2026-10-22 實測：700 tokens 直接空回應）。所以：① 預算放大 ② 帶上低思考量。
        effort = str(env_values.get("DEEPSEEK_THINKING") or "").strip()
        body_dict = {"model": model, "stream": False, "temperature": 0.3,
                     "max_tokens": int(max_tokens),
                     "messages": [{"role": "user", "content": prompt}]}
        from agent.brain.config import llm as _llm
        # 短任務不需要思考：思考會把 token 吃光（實測 420 tokens 直接空回應）。
        # ⚠️ DeepSeek 不認 `chat_template_kwargs`，要用 `thinking` / `reasoning_effort`
        #（2026-10-24 實測，見 `config/llm.thinking_payload`）。
        body_dict.update(_llm.thinking_payload(
            enable=not disable_thinking,
            effort=(effort if effort in {"none", "low", "high", "max"} else "low") if effort else "",
            base_url=base))
        body = json.dumps(body_dict).encode()
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        if key:
            request.add_header("Authorization", f"Bearer {key}")
        with urllib.request.urlopen(request, timeout=40) as response:
            data = json.loads(response.read().decode())
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("空回應")
        return str((choices[0].get("message") or {}).get("content") or "")

    return call


__all__ = ["TRAIT_DIMENSIONS", "TRIGGERS", "compile_core", "core_hash", "core_path",
           "http_call", "load_core", "parse_core", "prompt_block", "save_core"]
