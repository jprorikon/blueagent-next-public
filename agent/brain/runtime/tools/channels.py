"""工具的「渠道」：同一個她，在 App、電話、Discord、TRPG 桌上能用的工具不一樣。

owner 2026-10-31：「不是每一個渠道都需要帶這麼長的工具上下文」。工具面分三層：

1. **常駐**：沒宣告 `channels`、也不是 `on_demand` 的工具（記憶檢索／打開、`skill.view`）——哪裡都有。
2. **按渠道**：工具宣告 `channels = ("app", …)`，只在那些渠道出現、也只在那些渠道能執行
   （表情包只在 App；`voice.hangup` 只在通話裡……）。
3. **按需解鎖**：工具宣告 `on_demand = True` ⇒ 平常**不放進給模型的清單**，只在技能索引裡留一行；
   她 `skill.view` 載入某個技能後，那個技能 `requires: [tool:…]` 的工具才在這一輪後面的步驟出現。
   執行權限不變（可見性是上下文問題，不是安全問題）。

為什麼不只是省 token：工具一多，排在後面的等於沒有——2026-10-29 實測 `sticker.send` 排第 11 位時
12/12 輪都不調，只給 6 個工具時每次都調（`app/wiring.py`）。
"""

from __future__ import annotations

#: 渠道名（`channel_of` 的回傳值）
APP = "app"                 # App 聊天（打字、語音訊息、圖）
CALL = "call"               # 即時語音通話（App 單人通話、桌面麥克風）
DISCORD = "discord"         # Discord 文字
DISCORD_VC = "discord_vc"   # Discord 語音頻道
TRPG = "trpg"               # TRPG 桌
SELF = "self"               # 她自己的生活／自主工作（工具面另有白名單：`runtime/self_work.py`）

#: 即時語音通話的來源前綴（`app/turns.py` 的 VOICE_SOURCES 裡「真的在通話」的那幾個）
CALL_PREFIXES = ("phone:call", "mr:call", "voice:", "mic:")
#: 通話要快：只留結束通話與記憶檢索；技能解鎖也不能繞過這道渠道白名單。
CALL_ALLOW = frozenset({"voice.hangup", "memory.search"})


def channel_of(source: str) -> str:
    """`phone:owner` → `app`、`phone:call` → `call`、`discord:vc` → `discord_vc`……

    沒帶來源（`""`）＝ App 直接打 `/api/turn`（舊包不送 `X-Source`）⇒ 也算 App。
    認不得的來源回它自己的前綴（不屬於任何宣告過的渠道 ⇒ 只看得到不限渠道的工具）。
    """
    raw = str(source or "").strip().lower()
    if raw.startswith(CALL_PREFIXES):
        return CALL
    if raw.startswith("discord:vc"):
        return DISCORD_VC
    head = raw.split(":", 1)[0]
    # `mr`＝Quest 3 的 MR 端（自己一條管道，但工具面跟 App 一樣；通話那句已經在上面算成 call）
    if head in ("", "phone", "api", "mr"):
        return APP
    if head == "discord":
        return DISCORD
    if head in ("trpg", "table"):
        return TRPG
    return head

