"""按环境变量选择 BrainPort。

**没有默认值**（2026-10-25）：以前不设 `BLUEAGENT_BRAIN` 就悄悄跑 MockBrain——生产漏设一个变量，
她就开始用假话回主人，而且不报错。现在不设就启动失败；离线开发／测试要明写 `BLUEAGENT_BRAIN=mock`。
"""

from __future__ import annotations

import os
from typing import Mapping

from agent.brain.openai_compat import (OpenAICompatibleBrain,
                                                OpenAICompatibleConfig)
from agent.brain.mock import MockBrain
from agent.brain.domain.entity import EntityRegistry
from agent.brain.domain.principal import PrincipalRegistry
from agent.brain.config import llm as llm_config


#: 现役桥那套变量名 → 本仓的规范名。`BLUEAGENT_BRAIN=deepseek` 时照抄桥的配置，
#: 所以「换架构不换模型」：DEEPSEEK_BASE / DEEPSEEK_MODEL / DEEPSEEK_API_KEY / DEEPSEEK_THINKING。
PROVIDER_ENV: dict[str, tuple[tuple[str, str, str], ...]] = {
    "deepseek": (
        ("BLUEAGENT_LLM_BASE_URL", "DEEPSEEK_BASE", "https://api.deepseek.com/v1"),
        ("BLUEAGENT_LLM_MODEL", "DEEPSEEK_MODEL", "deepseek-v4-flash"),
        ("BLUEAGENT_LLM_API_KEY", "DEEPSEEK_API_KEY", ""),
    ),
}


def llm_settings(values: Mapping[str, str] | None = None, provider: str = "deepseek") -> dict:
    """**所有 LLM 呼叫點共用的一組解析**：base_url / model / api_key / thinking。

    解析順序：本仓规范名（`BLUEAGENT_LLM_*`）→ 桥那套名（`DEEPSEEK_*`）→ 默认值。
    `runtime/distill.py`、子代理、狀態引出都走這裡，避免「大腦用 A 模型、提煉用 B 模型」。
    """
    merged = _provider_values(provider, dict(values if values is not None else os.environ))
    return {
        "base_url": str(merged.get("BLUEAGENT_LLM_BASE_URL")
                        or "https://api.deepseek.com/v1").strip(),
        "model": str(merged.get("BLUEAGENT_LLM_MODEL") or "deepseek-v4-flash").strip(),
        "api_key": str(merged.get("BLUEAGENT_LLM_API_KEY") or "").strip(),
        "thinking": str(merged.get("BLUEAGENT_LLM_ENABLE_THINKING") or "0").strip(),
    }


def _provider_values(provider: str, values: Mapping[str, str]) -> dict[str, str]:
    """把 provider 専用变量補進本仓的规范名（已显式设了的规范名不动）。"""
    merged = dict(values)
    for canon, alt, default in PROVIDER_ENV.get(provider, ()):
        if not str(merged.get(canon) or "").strip():
            merged[canon] = str(merged.get(alt) or default).strip()
    return merged


def build_brain(entities: EntityRegistry, principals: PrincipalRegistry, *,
                delay: float = 0.35,
                env: Mapping[str, str] | None = None):
    values = dict(os.environ if env is None else env)
    provider = str(values.get("BLUEAGENT_BRAIN") or "").strip().lower()
    if not provider:
        raise ValueError(
            "BLUEAGENT_BRAIN is not set: use deepseek / openai / hermes for a real model, "
            "or BLUEAGENT_BRAIN=mock explicitly for offline development and tests")
    if provider in {"mock", "off"}:
        return MockBrain(entities, principals, delay=delay)
    if provider not in {"openai", "deepseek", "hermes", "compatible"}:
        raise ValueError(f"unknown BLUEAGENT_BRAIN: {provider}")
    # **大腦＝chat 角色**：額度／溫度／逾時由 `data/llm.json` 的 chat 決定，env 只在明寫時才蓋。
    # 從前這裡只讀 env（沒設就 512），所以 compose 裡那行 `BLUEAGENT_LLM_MAX_TOKENS: 400`
    # 一值定江山、改角色表看起來不生效（owner 2026-10-27 把 chat 定 3200 時踩到）。
    role = llm_config.settings_for("chat", env=values)
    # **大腦＝chat 角色：模型與端點也要跟著角色表**（2026-11-08 修）。
    # 以前這裡只把 max_tokens／temperature／timeout 從角色表搬進來，model／base_url 仍讀通用 env
    # ⇒ 改 `data/llm.json` 的 chat 看起來沒生效（2026-10-27 踩過一次）；2026-11-08 更直接踩到：
    #   通話切不回 flash（brain 仍在本機 125B 上打，第一句 3–7 s，被首字預算砍成空回）。
    # 只有**明寫**的才蓋（`BLUEAGENT_LLM_CHAT_*` 或 llm.json 的 chat）；都沒寫就照舊走通用 env／
    # provider 預設——不能拿 `settings_for` 的內建預設來蓋，否則 `DEEPSEEK_MODEL` 等全部失效。
    for key, value in llm_config.explicit_endpoint("chat", env=values).items():
        values["BLUEAGENT_LLM_MODEL" if key == "model" else "BLUEAGENT_LLM_BASE_URL"] = value
    for env_name, key in (("BLUEAGENT_LLM_MAX_TOKENS", "max_tokens"),
                          ("BLUEAGENT_LLM_TEMPERATURE", "temperature"),
                          ("BLUEAGENT_LLM_TIMEOUT_S", "timeout_s")):
        if str(values.get(env_name) or "").strip():
            continue                                  # env 明寫 ⇒ 尊重它（臨時覆寫用）
        if role.get(key) is None or str(role.get(key)).strip() == "":
            continue
        values[env_name] = str(role[key])
    config = OpenAICompatibleConfig.from_env(_provider_values(provider, values))
    if not llm_config.usable(config.base_url, config.api_key):
        raise ValueError(
            "BLUEAGENT_BRAIN requires BLUEAGENT_LLM_API_KEY "
            "(or DEEPSEEK_API_KEY) for a remote endpoint")
    return OpenAICompatibleBrain(config, entities, principals)


def brain_label(brain, env: Mapping[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    provider = str(values.get("BLUEAGENT_BRAIN") or "mock").strip().lower()
    if isinstance(brain, OpenAICompatibleBrain):
        return f"{provider or 'openai'}:{brain.config.model}"
    return "mock"


__all__ = ["build_brain", "brain_label"]
