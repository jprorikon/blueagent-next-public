"""共享世界層：對誰都一樣的事實（時刻／主人在哪個端／天氣／位置的 keyed 摘要）。

從 `world_delta.py` 拆出來（那個檔卡在 600 行棘輪）：`WorldDelta` 是「每個角色自己那一份」，
這裡是「一份給所有人」——兩者的更新節奏與隱私等級不同，分開放也比較不會互相污染。

`world_delta.py` 仍把這裡的名字轉出（既有 import 不必改）。
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import math
import os
import pathlib
import secrets
import time

#: 台帳的六個狀態（差量用）
_COUNT_KEYS = ("pending", "processing", "hanging", "locked", "outdated", "completed")
#: 這幾個狀態的變化值得說（outdated/completed 也算「片づいた」）
_LABELS = {"pending": "積まれた", "processing": "手をつけた", "hanging": "續きから",
           "locked": "返事待ち", "outdated": "手放した", "completed": "片づいた"}

#: 來源 → 人話（「主人在哪個端」用；只認前綴，`discord:vc` 要排在 `discord` 前面）
_DEVICES = (("discord:vc", "Discord の通話"), ("discord", "Discord"),
            ("phone:call", "スマホの通話"), ("phone", "スマホ"),
            ("app", "アプリ"), ("trpg", "TRPG"), ("api", "アプリ"))

#: 共享層允許的鍵（白名單：**只放對誰都一樣的事實**；位置只存 keyed digest）
SHARED_KEYS = ("at", "clock", "date", "part", "owner", "weather", "weather_code",
               "location_digest")

#: 一週的日文寫法
_WEEKDAYS = ("月", "火", "水", "木", "金", "土", "日")


def coarse_location_digest(row: dict | None, *, key: bytes = b"") -> str:
    """約 0.1° 網格的 HMAC；沒有密鑰不能列舉網格還原位置。無效輸入回空字串。"""
    if not isinstance(row, dict) or not key:
        return ""
    try:
        lat, lon = float(row["lat"]), float(row["lon"])
        if not (math.isfinite(lat) and math.isfinite(lon)
                and -90 <= lat <= 90 and -180 <= lon <= 180):
            return ""
        cell = f"{math.floor(lat * 10)},{math.floor(lon * 10)}"
    except (KeyError, TypeError, ValueError, OverflowError):
        return ""
    return hmac.new(key, cell.encode("ascii"), hashlib.sha256).hexdigest()[:16]


def _device_name(sources) -> str:
    """`sources`（一串來源名）→ 一句人話（他在用哪個端）；認不出回空字串。"""
    rows = [str(item) for item in (sources or ())]
    for key, name in _DEVICES:
        if any(item.startswith(key) for item in rows):
            return name
    return ""


def _part_of_day(hour: int) -> str:
    """她那邊現在是什麼時段（給模型一個「幾點」的語感，不進任何判定）。"""
    if 5 <= hour < 9:
        return "朝"
    if 9 <= hour < 12:
        return "昼前"
    if 12 <= hour < 15:
        return "昼"
    if 15 <= hour < 18:
        return "夕方"
    if 18 <= hour < 23:
        return "夜"
    return "深夜"


def _gap_text(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{int(seconds)} 秒"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} 分"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 時間{(' ' + str(rest) + ' 分') if rest else ''}"
    days, hours = divmod(hours, 24)
    return f"{days} 日{(' ' + str(hours) + ' 時間') if hours else ''}"


def _now_parts(at: float, tz: str) -> tuple[int, str, str]:
    """`(hour, "14:20", "11月3日(火)")`——用她的時區（容器是 UTC 也照她的算）。"""
    from agent.brain.runtime.inner.clocks import _zone      # noqa: PLC2701 - 同一層的時區表
    moment = datetime.datetime.fromtimestamp(float(at), _zone(tz))
    weekday = _WEEKDAYS[moment.weekday()]
    return (moment.hour, moment.strftime("%H:%M"),
            f"{moment.month}月{moment.day}日({weekday})")


class SharedWorld:
    """共享的世界變化：**定時刷新**，只放對誰都一樣的事實。

    資料源全部是別人的落盤（線上的活躍時間、來源名），不產生任何網路請求——
    她六十秒一步，這裡每步都打外網會變成燒錢機器。天氣要接就得先有快取，沒快取就不寫。
    """

    def __init__(self, root, *, threads=None, weather=None, locations=None, events=None,
                 clock=time.time, tz: str = "", interval_s: float = 300.0,
                 online_window_s: float = 600.0, stale_s: float = 1800.0,
                 max_chars: int = 240) -> None:
        self.root = pathlib.Path(root)
        self.threads = threads            # ThreadStore（只讀：誰最近在哪個端動過）
        self.weather = weather            # `() -> str|dict`：有快取才給，沒有就別接
        self.locations = locations        # LocationStore（只讀主人；共享層只留摘要）
        self.events = events              # WorldEvents：只發真正的 previous → current 變化
        self._clock = clock
        self._digest_key: bytes | None = None
        self.tz = str(tz or "")
        self.interval_s = max(30.0, float(interval_s))
        self.online_window_s = max(60.0, float(online_window_s))
        self.stale_s = max(self.interval_s, float(stale_s))
        self.max_chars = max(80, int(max_chars))

    # ── 落盤 ────────────────────────────────────────────────────────────────
    def path(self) -> pathlib.Path:
        return self.root / "shared.json"

    def _location_key(self) -> bytes:
        """同一安裝使用固定隨機密鑰；只進私有 data，0600，絕不進共享快照。"""
        if self._digest_key is not None:
            return self._digest_key
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "location-digest.key"
        try:
            key = path.read_bytes()
        except FileNotFoundError:
            key = secrets.token_bytes(32)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:         # 另一個執行緒先建好了，以落盤那份為準
                key = path.read_bytes()
            else:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(key)
        if len(key) != 32:
            raise ValueError("invalid location digest key")
        path.chmod(0o600)
        self._digest_key = key
        return key

    def read(self) -> dict:
        try:
            raw = json.loads(self.path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {key: value for key, value in raw.items() if key in SHARED_KEYS}

    def _save(self, facts: dict) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.path().write_text(json.dumps(facts, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    # ── 採樣 ────────────────────────────────────────────────────────────────
    def owner_state(self, now: float) -> dict:
        """主人在不在、在哪個端：看他最近在哪條線上動過（`ThreadStore` 只讀）。"""
        if self.threads is None:
            return {}
        try:
            rows = list(self.threads.threads())
        except Exception:                               # noqa: BLE001 - 取不到就不寫這一行
            return {}
        best = None
        for thread in rows:
            participants = getattr(thread, "participants", ()) or ()
            if "owner" not in participants:
                continue
            at = float(getattr(thread, "last_active_at", 0.0) or 0.0)
            if at <= 0:
                continue
            if best is None or at > best[0]:
                best = (at, thread)
        if best is None:
            return {}
        at, thread = best
        ago = max(0.0, float(now) - at)
        if ago > self.online_window_s:
            # 很久沒動 ⇒ 明確說「不在」，並給「最後一次」多久前（事實，不是猜）
            return {"online": False, "ago_s": int(ago),
                    "device": _device_name(getattr(thread, "sources", ()))}
        return {"online": True, "ago_s": int(ago),
                "device": _device_name(getattr(thread, "sources", ()))}

    def _publish_changes(self, previous: dict, current: dict) -> None:
        """只發 previous → current；第一次採樣只建基線，位置事件不含座標。"""
        if self.events is None or not previous:
            return
        before, after = previous.get("owner") or {}, current.get("owner") or {}
        if before and after:
            old = (bool(before.get("online")), str(before.get("device") or ""))
            new = (bool(after.get("online")), str(after.get("device") or ""))
            if old != new:
                if new[0] and old[0]:
                    text = f"主人が{old[1] or '別の端末'}から{new[1] or '別の端末'}に移った"
                elif new[0]:
                    text = f"主人が{new[1] or '端末'}に現れた"
                else:
                    text = "主人の気配が消えた"
                self.events.publish("owner_device", text)
        old_weather = previous.get("weather_code", previous.get("weather"))
        new_weather = current.get("weather_code", current.get("weather"))
        if old_weather not in (None, "") and new_weather not in (None, "") \
                and old_weather != new_weather:
            self.events.publish("weather", f"天気が変わった：{current.get('weather') or ''}",
                                public=True)
        old_digest = str(previous.get("location_digest") or "")
        new_digest = str(current.get("location_digest") or "")
        if old_digest and new_digest and old_digest != new_digest:
            self.events.publish("location", "主人のいる場所が変わった")

    def sample(self, now: float | None = None) -> dict:
        """採樣一次並落盤（白名單外的東西一律不放）。"""
        previous = self.read()
        moment = float(now if now is not None else self._clock())
        hour, clock_text, date_text = _now_parts(moment, self.tz)
        facts: dict = {"at": round(moment, 3), "clock": clock_text,
                       "date": date_text, "part": _part_of_day(hour)}
        owner = self.owner_state(moment)
        if owner:
            facts["owner"] = owner
        if self.weather is not None:
            try:
                current = self.weather()
                if isinstance(current, dict):
                    text = str(current.get("text") or "").strip()
                    code = current.get("code")
                else:
                    text, code = str(current or "").strip(), None
            except Exception:                           # noqa: BLE001 - 天氣拿不到就不寫
                text, code = "", None
            if text:
                facts["weather"] = text[:40]
            if code is not None:
                facts["weather_code"] = code
        if self.locations is not None:
            try:
                digest = coarse_location_digest(self.locations.latest(principal="owner"),
                                                key=self._location_key())
            except Exception:                           # noqa: BLE001 - 無資料不代表移動
                digest = ""
            if digest:
                facts["location_digest"] = digest
        facts = {key: value for key, value in facts.items() if key in SHARED_KEYS}
        self._publish_changes(previous, facts)
        self._save(facts)
        return facts

    def maybe_refresh(self, now: float | None = None) -> dict:
        """到 `interval_s` 才重採（定時刷新；每跳來問也沒關係）。"""
        moment = float(now if now is not None else self._clock())
        facts = self.read()
        if facts and moment - float(facts.get("at") or 0.0) < self.interval_s:
            return facts
        return self.sample(now=moment)

    # ── 給 prompt ───────────────────────────────────────────────────────────
    def block(self, now: float | None = None, *, public: bool = False) -> str:
        """世界那一小段（兩三行）；太久沒刷新就現採一次。

        `public=True`（產出會進公開筆記的那條路）：**不給主人的在線狀態／在哪個端**——
        那是主人的行蹤，寫進公開筆記就等於對誰都公開了（2026-11-03 review）。
        """
        moment = float(now if now is not None else self._clock())
        facts = self.read()
        if not facts or moment - float(facts.get("at") or 0.0) > self.stale_s:
            facts = self.sample(now=moment)
        if not facts:
            return ""
        lines = [f"［世界：{facts.get('date') or ''} {facts.get('clock') or ''}"
                 f"・{facts.get('part') or ''}］".replace("  ", " ")]
        owner = {} if public else (facts.get("owner") or {})
        if isinstance(owner, dict) and owner:
            device = str(owner.get("device") or "")
            ago = _gap_text(owner.get("ago_s") or 0)
            if owner.get("online"):
                lines.append(f"・主人はいま{device or 'どれかの端末'}にいる（{ago}前まで動いていた）")
            else:
                lines.append(f"・主人の気配はない（最後に動いたのは{ago}前"
                             + (f"・{device}" if device else "") + "）")
        weather = str(facts.get("weather") or "").strip()
        if weather:
            lines.append(f"・天気：{weather}")
        return "\n".join(lines)[:self.max_chars]


