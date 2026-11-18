"""天氣工具 `weather.get`（包 Open-Meteo 等外部服務，所以住 adapters；由組合根註冊）。"""

from __future__ import annotations

from agent.brain.runtime.tools.base import ToolResult


class GetWeatherTool:
    """天氣（`weather.get`）：沿用現役 BlueAgent 的 `get_weather`，含三條紅線。

      · **不唸經緯度**：座標只是查詢參數，不進台詞。
      · **地名不捏造**：只有結果裡有地名才說；沒有就只說天氣。
      · **IP 概算要聲明**：`coarse=True` 時結果會寫「IP からの概算」，她必須加一句「大概」。
    """

    name = "weather.get"
    #: 按需解鎖（技能 `weather`）
    on_demand = True
    schema = {
        "type": "function",
        "function": {
            "name": "weather_get",
            "description": (
                "オーナーの**今いる場所**の天気（現在の天気・気温・湿度・風、今日と明日の予報）を取る。"
                "「今日の天気は？」「寒い？」のように天気・気温・空模様の話になったら使う。"
                "位置は端末が送ってきているので、**場所を聞き返さなくてよい**。"
                "位置が無いときは IP からの**概算**で答え、結果に「IP からの概算」とあれば"
                "**概算だと一言添える**（正確を装わない）。"
                "⚠️ 結果の緯度経度は読み上げない。地名は結果に書いてあるときだけ言う——"
                "書いていなければ地名を推測して作らない。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "place": {"type": "string",
                              "description": ("省略可。会話で地名がはっきり言われたときだけ入れる"
                                              "（例「大阪の天気は？」→ 大阪）。"
                                              "今いる場所を聞かれただけなら空のまま。")},
                },
                "required": [],
            },
        },
    }

    def __init__(self, locations=None, client_ip: str = "") -> None:
        self.locations = locations
        self.client_ip = str(client_ip or "")

    def run(self, ctx, arguments: dict) -> "ToolResult":
        from agent.adapter import weather
        args = dict(arguments or {})
        place = str(args.get("place") or "").strip()
        point: dict | None = None
        if place:
            try:
                point = weather.geocode(place)
            except Exception as exc:                      # noqa: BLE001
                return ToolResult(ok=False,
                                  error=f"geocode_failed:{type(exc).__name__}")
            if point is None:
                return ToolResult(ok=False, error="place_not_found",
                                  data={"place": place})
        if point is None:
            located = self.locations.latest(
                principal=str(getattr(ctx, "principal", "") or ""),
                entity=str(getattr(ctx, "entity", "") or "")) \
                if self.locations is not None else None
            if located is not None:
                point = {"lat": located["lat"], "lon": located["lon"],
                         "label": weather.reverse_name(located["lat"], located["lon"]),
                         "coarse": False}
            else:
                ip_point = weather.ip_locate(weather.public_ip(self.client_ip))
                if ip_point is None:
                    return ToolResult(ok=False, error="no_location",
                                      data={"text": "位置が分からなかった（GPS も IP 概算も取れない）。"
                                                    "推測で天気を作らないこと。"})
                point = ip_point
        text = weather.describe(point["lat"], point["lon"],
                                label=str(point.get("label") or ""))
        if point.get("coarse"):
            text = "（IP からの概算）" + text
        return ToolResult(ok=True, data={"text": text,
                                         "label": str(point.get("label") or ""),
                                         "coarse": bool(point.get("coarse"))})


__all__ = ["GetWeatherTool"]
