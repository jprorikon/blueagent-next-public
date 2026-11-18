"""天氣：座標（或地名）→ 一句日語。**沿用現役 BlueAgent 的實作**（owner 2026-10-23）。

原版在 `docker/blueagent-server/ba/brain/weather.py`（現役橋還在用），這裡只做兩件事：
搬過來、把 I/O 形狀對齊 Next（位置由 `runtime/locations.py` 提供，不在這裡讀檔）。

刻意的選擇（照抄舊版的結論，別重新發明）：

* **零依賴、零鑰匙**：Open-Meteo 是公開 API，`urllib` 就夠；這個 image 不裝 `requests`。
* **地名不捏造**：只有呼叫端給了 `place=` 才回地名；座標反查地名是可選的（`WEATHER_REVERSE`），
  關掉時寧可只說天氣，也不猜「大概在東京」。
* **IP 概算是「概算」**：`public_ip()` 用 `is_global` 判（Python 的 `is_private` 會把 Tailscale 的
  100.64/10 判成 False，實測踩過）；`ip_locate()` 永不拋錯。
* **失敗要說失敗**：抓不到就回「取れなかった」，不要用猜的天氣填空。
"""

from __future__ import annotations

import ipaddress
import json
import os
import pathlib
import time
import urllib.parse
import urllib.request

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
REVERSE_URL = "https://api.bigdatacloud.net/data/reverse-geocode-client"
IP_URL = "http://ip-api.com/json/"

UA = "blueagent-next/1.0 (home LAN)"
TIMEOUT = 8.0
IP_TTL_S = 6 * 3600.0

#: 座標 → 地名（會把座標交給另一家）。不想就設 `WEATHER_REVERSE=off`。
WEATHER_REVERSE = str(os.environ.get("WEATHER_REVERSE") or "on").strip().lower() not in {
    "0", "off", "false", "no", ""}

WMO = {
    0: "快晴", 1: "晴れ", 2: "晴れ時々曇り", 3: "曇り",
    45: "霧", 48: "霧（着氷性）",
    51: "弱い霧雨", 53: "霧雨", 55: "強い霧雨",
    56: "着氷性の霧雨", 57: "強い着氷性の霧雨",
    61: "弱い雨", 63: "雨", 65: "強い雨",
    66: "着氷性の雨", 67: "強い着氷性の雨",
    71: "弱い雪", 73: "雪", 75: "強い雪", 77: "雪あられ",
    80: "弱いにわか雨", 81: "にわか雨", 82: "激しいにわか雨",
    85: "弱いにわか雪", 86: "強いにわか雪",
    95: "雷雨", 96: "雷雨（雹）", 99: "激しい雷雨（雹）",
}

_IP_CACHE: dict[str, tuple[float, dict | None]] = {}


def code_ja(code) -> str:
    try:
        return WMO.get(int(code), f"不明（code {code}）")
    except (TypeError, ValueError):
        return "不明"


def _get_json(url: str, params: dict | None = None, timeout: float = TIMEOUT) -> dict:
    query = urllib.parse.urlencode(params or {})
    target = f"{url}?{query}" if query else url
    request = urllib.request.Request(target, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:   # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def geocode(place: str) -> dict | None:
    """地名 → `{"label","lat","lon"}`；找不到回 None。"""
    place = (place or "").strip()
    if not place:
        return None
    data = _get_json(GEOCODE_URL, {"name": place, "count": 1, "language": "ja", "format": "json"})
    hits = data.get("results") or []
    if not hits:
        return None
    top = hits[0]
    label = " ".join(str(x) for x in (top.get("name"), top.get("admin1"), top.get("country")) if x)
    return {"label": label or place, "lat": float(top["latitude"]), "lon": float(top["longitude"])}


def reverse_name(lat: float, lon: float) -> str:
    """座標 → 地名（可選；失敗回空字串，絕不猜）。"""
    if not WEATHER_REVERSE:
        return ""
    try:
        data = _get_json(REVERSE_URL, {"latitude": lat, "longitude": lon,
                                       "localityLanguage": "ja"}, timeout=5.0)
        parts = [str(x) for x in (data.get("city") or data.get("locality"),
                                  data.get("principalSubdivision")) if x]
        return "、".join(parts)
    except Exception:                                   # noqa: BLE001 - 地名失敗不影響天氣
        return ""


def public_ip(ip: str) -> str:
    """只回「能拿去外部查」的 IP；私有／回環／CGNAT（含 Tailscale 100.64/10）回空字串。"""
    try:
        address = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return ""
    return str(address) if address.is_global else ""


def ip_locate(ip: str = "") -> dict | None:
    """IP → 城市級概算；**永不拋錯**，失敗回 None。"""
    key = (ip or "").strip() or "self"
    now = time.time()
    cached = _IP_CACHE.get(key)
    if cached and now - cached[0] < IP_TTL_S:
        return cached[1]
    out = None
    try:
        data = _get_json(IP_URL + ("" if key == "self" else key), {}, timeout=6.0)
        if data.get("status") == "success":
            out = {"lat": float(data["lat"]), "lon": float(data["lon"]),
                   "label": ", ".join(str(x) for x in (data.get("city"), data.get("regionName"))
                                      if x),
                   "ip": str(data.get("query") or key), "coarse": True}
    except Exception:                                   # noqa: BLE001
        out = None
    _IP_CACHE[key] = (now, out)
    return out


def forecast(lat: float, lon: float) -> dict:
    """現在 ＋ 今日／明日（一個請求）。"""
    return _get_json(FORECAST_URL, {
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min",
        "timezone": "auto", "forecast_days": 2,
    })


def _num(value, fmt: str = "{:.1f}", dash: str = "-") -> str:
    try:
        return fmt.format(float(value))
    except (TypeError, ValueError):
        return dash


def describe(lat: float, lon: float, label: str = "", stamp: str = "") -> str:
    """座標 → 一句日語（含現在與今日／明日）。抓不到就明說抓不到。"""
    try:
        data = forecast(lat, lon)
    except Exception as exc:                            # noqa: BLE001
        return f"天気を取れなかった（{type(exc).__name__}）。推測で答えないこと。"
    current = data.get("current") or {}
    daily = data.get("daily") or {}
    head = f"{stamp}の天気" if stamp else "天気"
    if label:
        head += f"（{label}）"
    lines = [f"{head}：{code_ja(current.get('weather_code'))}、"
             f"{_num(current.get('temperature_2m'), '{:.1f}')}℃"
             f"（体感 {_num(current.get('apparent_temperature'), '{:.1f}')}℃）、"
             f"湿度 {_num(current.get('relative_humidity_2m'), '{:.0f}')}%、"
             f"風 {_num(current.get('wind_speed_10m'), '{:.1f}')}km/h"]
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    codes = daily.get("weather_code") or []
    for index, name in enumerate(("今日", "明日")):
        if index < len(highs) and index < len(lows):
            weather = code_ja(codes[index]) if index < len(codes) else ""
            lines.append(f"{name}：{weather} 最高 {_num(highs[index])}℃／最低 {_num(lows[index])}℃")
    return "\n".join(lines)


class CachedWeather:
    """共享世界用的短天气：TTL 内不重查，失败沿用旧缓存，绝不猜天气。

    落盘只保留天气码／文字／采样时刻，不保留经纬度；坐标只用于当次 Open-Meteo 请求。
    """

    def __init__(self, locations, cache_path, *, ttl_s: float = 1800.0,
                 clock=time.time, fetch=forecast) -> None:
        import threading
        self.locations = locations
        self.path = pathlib.Path(cache_path)
        self.ttl_s = max(300.0, float(ttl_s))
        self._clock = clock
        self._fetch = fetch
        self._lock = threading.RLock()

    def _read(self) -> dict:
        try:
            row = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return row if isinstance(row, dict) and row.get("text") else {}

    def _write(self, row: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def current(self) -> dict:
        with self._lock:
            now = float(self._clock())
            cached = self._read()
            if cached and now - float(cached.get("at") or 0.0) < self.ttl_s:
                return cached
            try:
                location = self.locations.latest(principal="owner")
                if not location:
                    return cached
                data = self._fetch(float(location["lat"]), float(location["lon"]))
                current = data.get("current") or {}
                code = int(current["weather_code"])
                temperature = float(current["temperature_2m"])
                row = {"at": round(now, 3), "code": code,
                       "text": f"{code_ja(code)}、{temperature:.0f}℃"}
            except Exception:                           # noqa: BLE001 - 旧缓存比猜测可靠
                return cached
            self._write(row)
            return row

    def __call__(self) -> dict:
        return self.current()
