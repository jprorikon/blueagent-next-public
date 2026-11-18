"""兩個時鐘：「她的時間」與「對方的時間」（owner 2026-10-22）。

為什麼要分開：

  · **她的時間**＝她自己過日子的時區（作息、安靜時段、心跳的節奏）。這是**角色的屬性**，
    不是容器的屬性——容器跑在哪個時區跟她無關（實測踩過：容器是 UTC，於是 JST 白天
    也被判成「深夜，別說話」）。
  · **對方的時間**＝主人／guest 所在時區。拿來判「現在打擾他合不合適」。
    現在**還沒有地理資訊**（手機端接入後由定位推算），所以：
        `principal_tz()` 未知 ⇒ 回 ``""``，判定時**退回她的時間**，並在結果裡標
        `tz_source="entity"`（不是假裝知道對方時間）。

實作：一律用 `zoneinfo`，不碰 `time.localtime`（那會跟隨進程時區）。
"""

from __future__ import annotations

import os
import pathlib
import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: 她自己的時區（角色屬性；預設日本）。可用 `BLUEAGENT_ENTITY_TZ` 覆寫。
DEFAULT_ENTITY_TZ = "Asia/Tokyo"

#: 使用者的時區寫在 `data/users/<principal>.md` 的 identity 段裡，形如：
#:     tz: Asia/Shanghai
#: 沒有就當「未知」（不要猜）。
_TZ_LINE = re.compile(r"^\s*tz\s*[:：]\s*([A-Za-z0-9_+\-/]+)\s*$", re.MULTILINE)


def entity_tz() -> str:
    raw = (os.environ.get("BLUEAGENT_ENTITY_TZ") or DEFAULT_ENTITY_TZ).strip()
    return raw or DEFAULT_ENTITY_TZ


def _zone(name: str):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_ENTITY_TZ)


def hour_in(tz_name: str, at: float | None = None) -> int:
    """`at`（Unix 秒，None＝現在）在那個時區是幾點（0–23）。"""
    moment = datetime.fromtimestamp(at) if at is not None else datetime.now()
    return moment.astimezone(_zone(tz_name or DEFAULT_ENTITY_TZ)).hour


def principal_tz(principal: str, *, users_dir=None) -> str:
    """對方時區。**未知就回空字串**——不要用她的時區冒充對方的。"""
    if not principal or users_dir is None:
        return ""
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(principal)).strip("-") or "unknown"
    path = pathlib.Path(users_dir) / f"{slug}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    found = _TZ_LINE.search(text)
    return found.group(1).strip() if found else ""


def politeness(at: float | None, *, quiet_start: int = 23, quiet_end: int = 7,
               entity: str = "", principal: str = "", users_dir=None) -> dict:
    """現在打擾人合不合適。

    規矩：
      ① 有對方時區 ⇒ **以對方為準**（打擾的是他）。
      ② 沒有 ⇒ 退回**她的時區**（她也不該在自己深夜吵人），並標 `tz_source="entity"`。
      ③ 兩個人都在深夜 ⇒ 一定不打擾。
    """
    her = entity or entity_tz()
    his = principal_tz(principal, users_dir=users_dir)
    her_hour = hour_in(her, at)
    his_hour = hour_in(his, at) if his else her_hour

    def quiet(hour: int) -> bool:
        if quiet_start == quiet_end:
            return False
        return (quiet_start <= hour < quiet_end) if quiet_start < quiet_end \
            else (hour >= quiet_start or hour < quiet_end)

    blocked = quiet(his_hour) if his else quiet(her_hour)
    return {
        "quiet": bool(blocked),
        "tz_source": "principal" if his else "entity",
        "entity_tz": her, "entity_hour": her_hour,
        "principal_tz": his, "principal_hour": his_hour,
    }


__all__ = ["DEFAULT_ENTITY_TZ", "entity_tz", "hour_in", "politeness", "principal_tz"]
