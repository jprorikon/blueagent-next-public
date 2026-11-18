"""按角色凭证读取 App Server 授权位置；旧 MR 位置仍走原 Agent 端点。"""

from __future__ import annotations

import math
import os
import time

from agent.adapter.app_server import AppServerClient, _tokens


class PlatformLocationStore:
    """LocationStore 兼容面；实体上下文绝不借用其他角色的授权。

    `entity=""` 是 SharedWorld 的旧共享 MR 路径，保持原样。实体天气查询只合并
    自己的 App Server 授权与显式标记为 MR 的旧位置，避免旧手机直报在撤销后复活。
    """

    def __init__(self, legacy, clients: dict[str, AppServerClient], *, clock=time.time) -> None:
        self.legacy = legacy
        self.clients = dict(clients)
        self._clock = clock

    def save(self, *args, **kwargs):
        return self.legacy.save(*args, **kwargs)

    def get(self, device: str):
        return self.legacy.get(device)

    def snapshot(self):
        return self.legacy.snapshot()

    @staticmethod
    def _mr_only(row) -> dict | None:
        if not isinstance(row, dict):
            return None
        return row if str(row.get("source") or "").startswith("mr:") else None

    def _remote(self, entity: str, principal: str):
        client = self.clients.get(str(entity))
        if client is None:
            return None
        try:
            row = (client.board_call("location.get", {"principal": principal}) or {}).get("location")
            if not isinstance(row, dict):
                return None
            lat, lon = float(row["lat"]), float(row["lon"])
            at, expires = float(row["at"]), float(row["expires_at"])
            if not all(math.isfinite(value) for value in (lat, lon, at, expires)) \
                    or not (-90 <= lat <= 90 and -180 <= lon <= 180) or expires <= self._clock():
                return None
            # 即使远端被替换，Agent 侧也只接受 coarse 契约，不接受精确位置或无限期样本。
            accuracy = float(row.get("accuracy") or 0)
            if str(row.get("precision") or "") != "coarse" \
                    or not math.isfinite(accuracy) or accuracy < 1000:
                return None
            return row
        except Exception:  # 网络／授权失败回退 MR 或 IP，不伪造 App 当前位置
            return None

    def _latest_mr(self, principal: str) -> dict | None:
        # 不能用 legacy.latest() 后再过滤：更新的旧手机样本会把仍有效的 MR 样本遮成 None。
        rows = (self.legacy.snapshot() or {}).get("locations") or []
        candidates = []
        for row in rows:
            mr = self._mr_only(row)
            if mr is None or (principal and str(mr.get("principal") or "") != principal):
                continue
            try:
                float(mr.get("at") or 0.0)
            except (TypeError, ValueError):
                continue
            candidates.append(mr)
        return max(candidates, key=lambda row: float(row.get("at") or 0.0)) \
            if candidates else None

    def latest(self, *, principal: str = "", entity: str = "") -> dict | None:
        who = str(principal or "")
        if not entity:
            return self.legacy.latest(principal=who)
        local = self._latest_mr(who)
        remote = self._remote(str(entity), who)
        candidates = [row for row in (local, remote) if row is not None]
        if not candidates:
            return None
        return max(candidates, key=lambda row: float(row.get("at") or 0.0))


def build_location_store(legacy, env=None):
    env = os.environ if env is None else env
    base_url = str(env.get("BLUEAGENT_APP_SERVER_URL") or "").strip()
    tokens = _tokens(str(env.get("BLUEAGENT_APP_SERVER_TOKENS") or ""))
    if not base_url or not tokens:
        return legacy
    clients = {entity: AppServerClient(base_url, token, timeout=5.0)
               for entity, token in tokens.items()}
    return PlatformLocationStore(legacy, clients)


__all__ = ["PlatformLocationStore", "build_location_store"]
