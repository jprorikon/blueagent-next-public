"""独立 App Server 配置；数据与账号文件默认独立于 Agent data/。"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass


def _seconds(raw, default: float) -> float:
    try:
        return max(0.0, float(raw)) if str(raw or "").strip() else default
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class AppConfig:
    host: str
    port: int
    data_root: pathlib.Path
    accounts_path: pathlib.Path
    brain_url: str = ""
    voice_token: str = ""
    #: 推送叫醒（`docs/PLAN-app-kick-20261108.md`）：留空＝功能關（退回 Agent 側輪詢）
    kick_url: str = ""
    kick_token: str = ""
    #: Agent 問不到時，人類 token 上次確認「有效」還能用多久（審核 R1；`device_auth.py`）
    auth_grace_s: float = 600.0
    client_assets: pathlib.Path = pathlib.Path(__file__).resolve().parents[1] / "client" / "assets"

    @classmethod
    def from_env(cls, env=None) -> "AppConfig":
        env = os.environ if env is None else env
        data_root = pathlib.Path(env.get("BLUEAGENT_APP_DATA") or "data/app-server").resolve()
        accounts = pathlib.Path(env.get("BLUEAGENT_APP_ACCOUNTS") or data_root / "accounts.json").resolve()
        client_assets = pathlib.Path(
            env.get("BLUEAGENT_APP_CLIENT_ASSETS")
            or (pathlib.Path(__file__).resolve().parents[1] / "client" / "assets")).resolve()
        try:
            port = int(env.get("BLUEAGENT_APP_PORT") or "8788")
        except ValueError as exc:
            raise ValueError("BLUEAGENT_APP_PORT must be an integer") from exc
        if not 0 <= port <= 65535:
            raise ValueError("BLUEAGENT_APP_PORT must be between 0 and 65535")
        host = str(env.get("BLUEAGENT_APP_HOST") or "127.0.0.1").strip()
        if not host:
            raise ValueError("BLUEAGENT_APP_HOST must not be empty")
        return cls(host=host, port=port, data_root=data_root, accounts_path=accounts,
                   brain_url=str(env.get("BLUEAGENT_BRAIN_URL") or "").strip().rstrip("/"),
                   voice_token=str(env.get("BLUEAGENT_APP_VOICE_TOKEN") or "").strip(),
                   kick_url=str(env.get("BLUEAGENT_APP_KICK_URL") or "").strip(),
                   kick_token=str(env.get("BLUEAGENT_APP_KICK_TOKEN") or "").strip(),
                   auth_grace_s=_seconds(env.get("BLUEAGENT_APP_AUTH_GRACE_S"), 600.0),
                   client_assets=client_assets)


__all__ = ["AppConfig"]
