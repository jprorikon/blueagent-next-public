"""LLM 角色設定：**一個檔決定每個角色用什麼模型**（owner 2026-10-23「列出來再配置」）。

以前模型散在 compose env、secrets、各模組預設值裡，換一顆就要翻四個地方。現在收成一張表：

    data/llm.json
    {
      "roles": {
        "chat":     {"model": "deepseek-flash", "max_tokens": 400,  "temperature": 0.8},
        "state":    {"model": "deepseek-flash", "max_tokens": 1200},
        "subagent": {"model": "deepseek-flash", "max_tokens": 1200},
        "distill":  {"model": "deepseek-flash", "max_tokens": 800,  "temperature": 0.2},
        "vision":   {"model": "deepseek-flash", "max_tokens": 400,  "temperature": 0.2},
        "moodline":  {"model": "deepseek-flash", "max_tokens": 300,  "temperature": 0.3},
        "monologue": {"model": "deepseek-flash", "max_tokens": 700,  "temperature": 0.6}
      }

解析順序（**低到高：預設 → 檔案 → 通用 env → 角色專屬 env**）：
    內建預設 → `data/llm.json` 的 role → `BLUEAGENT_LLM_*`／`DEEPSEEK_*` → `BLUEAGENT_LLM_<ROLE>_*`
    （檔案是「預設值」，env 是「臨時覆寫」，角色專屬 env 是「只改這一個角色」的逃生口）

刻意不做的事：
  · 不把 API key 寫進檔案（key 只住 `.secrets/`，這裡只放「用哪顆、多少預算」）。
  · 檔案不在 ⇒ 完全照舊行為（純 env），所以壞了不會炸服務。
"""

from __future__ import annotations

import json
import os
import pathlib

DEFAULT_ROLES: dict[str, dict] = {
    "chat": {"model": "deepseek-flash", "base_url": "https://api.deepseek.com/v1",
             "max_tokens": 400, "temperature": 0.8, "timeout_s": 60.0},
    "state": {"model": "deepseek-flash", "max_tokens": 1200, "temperature": 0.3},
    "subagent": {"model": "deepseek-flash", "max_tokens": 1200, "temperature": 0.9},
    "distill": {"model": "deepseek-flash", "max_tokens": 800, "temperature": 0.2},
    "vision": {"model": "deepseek-flash", "max_tokens": 400, "temperature": 0.2,
               "timeout_s": 120.0},
    #: 心跳的兩支小活（owner 2026-11-02）：**不要跟主腦搶那條唯一的推論 slot**。
    #: 心情定位＝把 v／a 翻成一句人話（很短）；內心獨白＝一拍一段（比定位長一點）。
    #: 這裡只放「預設」；實際端點在 compose 的 `BLUEAGENT_LLM_MOODLINE_*`／`..._MONOLOGUE_*`。
    "moodline": {"model": "deepseek-flash", "max_tokens": 300, "temperature": 0.3},
    "monologue": {"model": "deepseek-flash", "max_tokens": 700, "temperature": 0.6},
    #: 出口閘的評審（common-ground 第 6 步）：站在主人的位置讀一句、回 JSON。短、冷靜。
    "premise": {"model": "deepseek-flash", "max_tokens": 200, "temperature": 0.0},
}

#: role 的 env 前綴（`BLUEAGENT_LLM_STATE_MODEL` 之類可以單獨覆寫某個角色）
ROLE_ENV_PREFIX = "BLUEAGENT_LLM_"


def config_path(root=None) -> pathlib.Path:
    if root is not None:
        return pathlib.Path(root) / "llm.json"
    explicit = str(os.environ.get("BLUEAGENT_LLM_CONFIG") or "").strip()
    if explicit:
        return pathlib.Path(explicit)
    data = str(os.environ.get("BLUEAGENT_DATA_DIR") or "").strip()
    base = pathlib.Path(data) if data else pathlib.Path("data")
    return base / "llm.json"


def load(root=None) -> dict[str, dict]:
    """讀 `data/llm.json`；缺檔／壞檔都回空 dict（呼叫端自己退回 env）。"""
    path = config_path(root)
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    roles = body.get("roles") if isinstance(body, dict) else None
    return {str(key): dict(value) for key, value in (roles or {}).items()
            if isinstance(value, dict)}


def _env_settings(env) -> dict:
    def pick(*names: str) -> str:
        for name in names:
            value = str(env.get(name) or "").strip()
            if value:
                return value
        return ""

    # 只回「env 真的有給」的欄位——沒給就讓檔案／預設值留下來（空字串會被略過）
    return {
        "base_url": pick("BLUEAGENT_LLM_BASE_URL", "DEEPSEEK_BASE"),
        "model": pick("BLUEAGENT_LLM_MODEL", "DEEPSEEK_MODEL"),
        "api_key": pick("BLUEAGENT_LLM_API_KEY", "DEEPSEEK_API_KEY"),
    }


def settings_for(role: str, *, env=None, root=None) -> dict:
    """回某個角色實際生效的設定（預設 → 檔案 → 通用 env → 角色專屬 env）。"""
    env = os.environ if env is None else env
    base = dict(DEFAULT_ROLES.get(str(role), DEFAULT_ROLES["chat"]))
    for key, value in (load(root).get(str(role)) or {}).items():
        if value is not None and str(value).strip() != "":
            base[key] = value
    for key, value in _env_settings(env).items():
        if str(value or "").strip():
            base[key] = value
    prefix = f"{ROLE_ENV_PREFIX}{str(role).upper()}_"
    for key, env_name in (("model", "MODEL"), ("base_url", "BASE_URL"),
                          ("max_tokens", "MAX_TOKENS"), ("temperature", "TEMPERATURE"),
                          ("timeout_s", "TIMEOUT_S")):
        value = str(env.get(prefix + env_name) or "").strip()
        if value:
            base[key] = value
    base.setdefault("base_url", DEFAULT_ROLES["chat"]["base_url"])
    base.setdefault("model", DEFAULT_ROLES["chat"]["model"])
    base.setdefault("timeout_s", DEFAULT_ROLES["chat"].get("timeout_s", 60.0))
    try:
        base["max_tokens"] = int(base.get("max_tokens") or 0)
    except (TypeError, ValueError):
        base["max_tokens"] = DEFAULT_ROLES["chat"]["max_tokens"]
    for key in ("temperature", "timeout_s"):
        try:
            base[key] = float(base.get(key) or 0)
        except (TypeError, ValueError):
            base[key] = float(DEFAULT_ROLES["chat"].get(key) or 0.0)
    base["role"] = str(role)
    base["api_key"] = str(base.get("api_key") or "")
    return base


def explicit_endpoint(role: str, *, env=None, root=None) -> dict:
    """只回「有人明寫」的 model／base_url：角色專屬 env 優先，其次 `data/llm.json` 的該角色。

    **不含內建預設、也不含通用 env**——`settings_for` 一定會補上預設（`deepseek-flash`），
    拿它來蓋主腦的端點，會把 `DEEPSEEK_MODEL`／provider 預設全部吃掉（2026-11-08 的回歸）。
    """
    env = os.environ if env is None else env
    entry = load(root).get(str(role)) or {}
    prefix = f"{ROLE_ENV_PREFIX}{str(role).upper()}_"
    picked = {}
    for key, suffix in (("model", "MODEL"), ("base_url", "BASE_URL")):
        value = str(env.get(prefix + suffix) or "").strip() or str(entry.get(key) or "").strip()
        if value:
            picked[key] = value
    return picked


def summary(*, env=None, root=None) -> list[dict]:
    """啟動要印的那張表：每個角色實際會用什麼。"""
    env = os.environ if env is None else env
    rows = []
    for role in sorted(set(DEFAULT_ROLES) | set(load(root))):
        item = settings_for(role, env=env, root=root)
        rows.append({"role": role, "model": item["model"], "base": item["base_url"],
                     "max_tokens": item["max_tokens"],
                     "temperature": item["temperature"]})
    return rows


def apply_env_defaults(*, env=None, root=None) -> dict:
    """把 `chat` 角色的設定寫回 `BLUEAGENT_LLM_*`（讓大腦與 http_call 一起吃到）。

    只補「還沒設」的欄位，不覆蓋 compose 明確給的值。
    """
    env = os.environ if env is None else env
    chat = settings_for("chat", env=env, root=root)
    if not str(env.get("BLUEAGENT_LLM_MODEL") or "").strip():
        env["BLUEAGENT_LLM_MODEL"] = str(chat["model"])
    if not str(env.get("BLUEAGENT_LLM_BASE_URL") or "").strip():
        env["BLUEAGENT_LLM_BASE_URL"] = str(chat["base_url"])
    return chat


#: 不需要金鑰的端點（本機 Hermes／vLLM／容器連宿主）。跟 `brain/factory.py` 同一張表。
LOCAL_HOSTS = ("127.0.0.1", "localhost", "host.docker.internal",
               # 2026-11-02 本地化：R9700 上的 Strata（192.168.50.36）與 9070XT 上的 llama.cpp（8091/8092）
               # 都在內網。少了這些前綴，`thinking_payload` 會誤判成遠端，改送 DeepSeek 的
               # `thinking:{type:disabled}`——本機引擎不認，於是每通呼叫都燒滿 688 token 的思考。
               "192.168.", "10.", "172.16.", "172.17.", "172.18.", "172.19.",
               "172.2", "172.30.", "172.31.")


def is_local(base_url: str) -> bool:
    return any(token in str(base_url or "") for token in LOCAL_HOSTS)


def usable(base_url: str, api_key: str) -> bool:
    """這組設定**能不能真的打出去**：遠端要有 key，本機不用。

    沒 key 的遠端呼叫只會換來 401，卻要付一次網路往返（還曾在「離線」測試裡打出幾百次）。
    呼叫端拿到 False 就當「這個通道沒有」，各階段本來就會自己跳過。
    """
    return bool(str(api_key or "").strip()) or is_local(base_url)


def thinking_payload(*, enable: bool, effort: str = "", base_url: str = "") -> dict:
    """「要不要思考」的正確寫法（2026-10-24 查官方文件＋實測）。

    DeepSeek Chat Completions 的官方參數是
      · `thinking: {"type": "enabled"|"disabled"}`（預設 **enabled**）
      · `reasoning_effort: "none"|"low"|"high"|"max"`（預設 **high**；`none`＝關思考）
    ⚠️ `chat_template_kwargs: {"enable_thinking": false}` 是 vLLM/Qwen 的鍵，**DeepSeek 忽略它**
    ⇒ 我們以前以為關掉了思考，其實每一輪都在 high 努力度下燒 reasoning
    （實測：同一句 reasoning 228–592 tokens、多花 1.5–2.5 s）。

    實測（deepseek-flash，同一 prompt）：
      · 現在送法（chat_template_kwargs）  2.80 s、reasoning 228
      · thinking disabled                 1.28 s、reasoning 0
      · reasoning_effort=none             1.11 s、reasoning 0

    本機端點（vLLM／llama-server）沒有 DeepSeek 那兩個參數，仍走 `chat_template_kwargs`。
    """
    body: dict = {}
    local = is_local(base_url)
    if enable:
        if effort:
            body["reasoning_effort"] = effort if effort in {"none", "low", "medium", "high", "max"} else "high"
        return body
    if local:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    else:
        body["thinking"] = {"type": "disabled"}
    return body
