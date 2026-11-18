"""36K 預算下滾動段實際能放幾條（用真實長度的句子估，只看組裝結果）。"""
import os

from agent.brain.messages import build_messages
from agent.brain.openai_compat import OpenAICompatibleConfig
from agent.brain.ports import BrainRequest

config = OpenAICompatibleConfig.from_env(os.environ)
print("budget:", config.max_chars, "max_history:", config.max_history)

# 日語語音轉寫常見 20–60 字；assistant 回覆 40–120 字
rows = []
for index in range(40):
    if index % 2 == 0:
        text = "あ" * 40 + f"（{index:03d}）"
    else:
        text = "い" * 90 + f"（{index:03d}）"
    rows.append({"role": "user" if index % 2 == 0 else "assistant", "text": text})

request = BrainRequest(
    thread_id="th_probe", entity="mira", principal="owner", text="じゃあ続きを話すね",
    system="persona" * 20,
    meta={"transcript": rows, "carry_summary": "前に小説の話と TRPG の話をした",
          "speak_lang": "ja"},
)
messages = build_messages(request, max_history=config.max_history,
                          max_chars=config.max_chars)
total = sum(len(str(item.get("content") or "")) for item in messages)
history = [item for item in messages[1:-1]]
print("messages:", len(messages), "history kept:", len(history),
      "total chars:", total, "/", config.max_chars)
print("oldest kept:", str(history[0]["content"])[-6:] if history else "-",
      "newest kept:", str(history[-1]["content"])[-6:] if history else "-")
print("carry in prompt:", any("[[before]]" in str(item.get("content")) for item in messages))
