"""App 的端末：手機（`phone`）與 Quest 3 的 MR 頭顯（`mr`）——**各走各的管道**（owner 2026-11-01）。

以前 MR 用的是手機那支 `NextApi`、來源一律算成手機 ⇒ 兩端混在一起：
MR 的對話跑進手機的聊天紀錄、推播兩邊一起響、MR 撥電話拿到的是手機那一通。
現在用 `X-Source: mr` 分開：

  · 來源標籤：MR 的通話一句＝`mr:call`（手機是 `phone:call`）；手機查紀錄只看 `phone`／`api` ⇒ 自然看不到 MR。
  · 推播：**照來源送回那一端**（`mr:` 開頭的只給 MR，其他——包括主動訊息——只給手機）。
  · 通話：一個角色同時只有一通；**後撥的那一端蓋掉先撥的**（owner 2026-11-01「通話都以後啟動管道覆蓋前管道」）。
"""

from __future__ import annotations

PHONE = "phone"
MR = "mr"
#: App 的端末（通話「後撥蓋掉先撥」只在這幾個之間；Discord 語音房不算）
APP_ENDS: tuple[str, ...] = (PHONE, MR)
#: 通話裡說的那一句的來源
CALL_SOURCES: tuple[str, ...] = (f"{PHONE}:call", f"{MR}:call")


def end_of(x_source: str) -> str:
    """請求標頭 `X-Source`（或 `?end=`）→ 哪一端。沒帶／認不得 ⇒ 手機（舊包不送這個標頭）。"""
    return MR if str(x_source or "").strip().lower().split(":", 1)[0] == MR else PHONE


def end_of_source(source: str) -> str:
    """一輪的來源標籤 → 它的推播該送到哪一端（`mr:` 開頭＝MR，其他一律手機）。"""
    return MR if str(source or "").strip().lower().startswith(f"{MR}:") else PHONE


def call_source(end: str) -> str:
    """這一端通話裡說的那一句的來源標籤。"""
    return f"{MR if end == MR else PHONE}:call"


def is_call(source: str) -> bool:
    """這一輪是 App（手機或 MR）通話裡說的。"""
    return str(source or "").strip().lower().startswith(CALL_SOURCES)


__all__ = ["APP_ENDS", "CALL_SOURCES", "MR", "PHONE", "call_source", "end_of", "end_of_source",
           "is_call"]
