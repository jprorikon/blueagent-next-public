"""技能整理／學習專用腦（owner 2026-11-03）：長上下文、沒有回話延遲壓力 ⇒ 走雲端 DeepSeek。"""

from __future__ import annotations

import os

def build_skills_brain(entities, principals, llm_config=None):
    """技能整理／學習專用腦（owner 2026-11-03）：長上下文、沒有回話延遲壓力 ⇒ 走雲端 DeepSeek。

    `BLUEAGENT_SKILLS_BRAIN=local` 一鍵回滾（回 None ⇒ 全部走本地）。
    """
    if str(os.environ.get("BLUEAGENT_SKILLS_BRAIN") or "deepseek").strip().lower() == "local":
        return None
    try:
        from agent.brain.openai_compat import OpenAICompatibleBrain, OpenAICompatibleConfig
        # ⚠️ 不能用 `settings_for`：它會套用通用 env（BLUEAGENT_LLM_MODEL／BASE_URL），
        # 那兩個是給主腦指向本地 7900XTX 的 ⇒ 技能腦會被一起拉回本地（實測踩到）。
        # 技能腦只認 `data/llm.json` 裡的 `skills` 角色。
        from agent.brain.config import llm as llm_config
        role = dict((llm_config.load().get("skills") or {}))
        key = str(os.environ.get("DEEPSEEK_API_KEY") or "")
        if not (role.get("base_url") and key):
            return None
        brain = OpenAICompatibleBrain(
            OpenAICompatibleConfig(base_url=str(role.get("base_url")),
                                   model=str(role.get("model") or "deepseek-chat"),
                                   api_key=key,
                                   max_tokens=int(role.get("max_tokens") or 1400),
                                   max_chars=120000,
                                   temperature=float(role.get("temperature") or 0.6),
                                   timeout_s=float(role.get("timeout_s") or 90),
                                   enable_thinking=False),
            entities, principals)
        print(f"  brain[skills]: {brain.config.model}", flush=True)
        return brain
    except Exception as exc:                       # noqa: BLE001 - 建不起來就當沒有
        print(f"  brain[skills]: 建不起來（{type(exc).__name__}）⇒ 全部走本地", flush=True)
        return None


