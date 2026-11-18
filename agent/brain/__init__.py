"""真实 BrainPort 的实现：OpenAI 兼容流式聊天 + prompt 组装。"""

from agent.brain.factory import build_brain
from agent.brain.messages import build_messages
from agent.brain.openai_compat import (OpenAICompatibleBrain,
                                                OpenAICompatibleConfig)

__all__ = ["OpenAICompatibleBrain", "OpenAICompatibleConfig",
           "build_brain", "build_messages"]
