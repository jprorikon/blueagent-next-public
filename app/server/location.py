"""明确授权的 coarse 设备位置；独立保存，不导入 Agent 或社交消息库。"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time

from app.server.events import append_event
from app.server.social.models import Actor, BoardError

PURPOSE = "weather-v1"
PRECISION_M = 1000
TTL_S = 1800.0
_DEVICE = re.compile(r"[A-Za-z0-9_-]{8,64}")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS location_consent(
  account_id TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 0,
  purpose TEXT NOT NULL,
  authorized_accounts TEXT NOT NULL DEFAULT '[]',
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS location_sample(
  account_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  lat REAL NOT NULL,
  lon REAL NOT NULL,
  accuracy_m REAL NOT NULL,
  observed_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  source TEXT NOT NULL,
  PRIMARY KEY(account_id, device_id)
);
CREATE INDEX IF NOT EXISTS location_sample_expiry ON location_sample(expires_at);
"""


def _coarse(value, *, latitude: bool) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise BoardError(400, "bad_coordinates") from None
    limit = 90.0 if latitude else 180.0
    if not math.isfinite(number) or not -limit <= number <= limit:
        raise BoardError(400, "bad_coordinates")
    # 客户端也粗化，但服务端绝不信任客户端精度；两位小数约 1.1 km 纬度网格。
    return round(number, 2)


def _pack(row, account) -> dict:
    return {
        "account_id": account.id,
        "principal": account.legacy_principal or account.id,
        "device": row["device_id"],
        "lat": float(row["lat"]),
        "lon": float(row["lon"]),
        "accuracy": max(PRECISION_M, float(row["accuracy_m"])),
        "at": float(row["observed_at"]),
        "expires_at": float(row["expires_at"]),
        "source": row["source"],
        "precision": "coarse",
        "purpose": PURPOSE,
    }


class LocationService:
    """授权是持久设置；坐标样本独立限时，撤销立即删除且不可再读。"""

    def __init__(self, db_path, accounts, *, now=time.time, ttl_s: float = TTL_S) -> None:
        self.db_path = str(db_path)
        self.accounts = accounts
        self.now = now
        self.ttl_s = max(60.0, float(ttl_s))
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _account(self, actor: Actor):
        account = self.accounts.get(actor.id)
        if account is None:
            raise BoardError(403, "unknown_account")
        return account

    @staticmethod
    def _grants(row) -> list[str]:
        try:
            values = json.loads(row["authorized_accounts"] if row is not None else "[]")
        except (TypeError, ValueError):
            return []
        return sorted({str(item) for item in values if str(item)}) \
            if isinstance(values, list) else []

    def _purge_expired(self, now: float) -> None:
        self._db.execute("DELETE FROM location_sample WHERE expires_at<=?", (now,))

    def status(self, actor: Actor) -> dict:
        account = self._account(actor)
        now = float(self.now())
        with self._lock, self._db:
            self._purge_expired(now)
            consent = self._db.execute(
                "SELECT * FROM location_consent WHERE account_id=?", (account.id,)).fetchone()
            devices = self._db.execute(
                "SELECT device_id,observed_at,expires_at,source FROM location_sample "
                "WHERE account_id=? ORDER BY observed_at DESC", (account.id,)).fetchall()
        enabled = bool(consent is not None and consent["enabled"])
        return {"sharing": {
            "enabled": enabled,
            "purpose": PURPOSE,
            "precision": "coarse",
            "precision_m": PRECISION_M,
            "ttl_s": int(self.ttl_s),
            "authorized_account_ids": self._grants(consent) if enabled else [],
            "updated_at": float(consent["updated_at"]) if consent is not None else 0.0,
            "devices": [{"device": row["device_id"], "at": float(row["observed_at"]),
                         "expires_at": float(row["expires_at"]), "source": row["source"]}
                        for row in devices] if enabled else [],
        }}

    def set_sharing(self, actor: Actor, enabled) -> dict:
        account = self._account(actor)
        if account.kind == "entity":
            raise BoardError(403, "location_human_only")
        if not isinstance(enabled, bool):
            raise BoardError(400, "invalid_location_consent")
        now = float(self.now())
        event_id = ""
        with self._lock, self._db:
            previous = self._db.execute(
                "SELECT * FROM location_consent WHERE account_id=?", (account.id,)).fetchone()
            was_enabled = bool(previous is not None and previous["enabled"])
            grants = [item.id for item in self.accounts.all() if item.kind == "entity"] \
                if enabled else []
            self._db.execute("""
              INSERT INTO location_consent(account_id,enabled,purpose,authorized_accounts,updated_at)
              VALUES (?,?,?,?,?)
              ON CONFLICT(account_id) DO UPDATE SET enabled=excluded.enabled,
                purpose=excluded.purpose,authorized_accounts=excluded.authorized_accounts,
                updated_at=excluded.updated_at
            """, (account.id, int(enabled), PURPOSE, json.dumps(grants), now))
            if not enabled:
                # 撤销同时删除所有设备样本；旧事件不含新坐标，也不再向 Agent 提供当前位置。
                self._db.execute("DELETE FROM location_sample WHERE account_id=?", (account.id,))
                if was_enabled:
                    event_id = append_event(
                        self._db, event_type="location.revoked", at=now,
                        actor_id=account.id, actor_kind=account.kind,
                        entity_kind="account", entity_id=account.id,
                        payload={"account_id": account.id, "purpose": PURPOSE},
                        audience=[account.id, *self._grants(previous)])
        result = self.status(account.actor())
        if event_id:
            result["event_id"] = event_id
        return result

    def report(self, actor: Actor, body: dict) -> dict:
        account = self._account(actor)
        if account.kind == "entity":
            raise BoardError(403, "location_human_only")
        device = str(body.get("device") or "")
        if not _DEVICE.fullmatch(device):
            raise BoardError(400, "invalid_location_device")
        lat = _coarse(body.get("lat"), latitude=True)
        lon = _coarse(body.get("lon"), latitude=False)
        try:
            accuracy = float(body.get("accuracy") or PRECISION_M)
        except (TypeError, ValueError):
            accuracy = PRECISION_M
        if not math.isfinite(accuracy):
            accuracy = PRECISION_M
        accuracy = max(PRECISION_M, min(100000.0, accuracy))
        source = str(body.get("source") or "").strip().lower()
        if source not in {"ios", "android"}:
            raise BoardError(400, "invalid_location_source")
        now = float(self.now())
        expires = now + self.ttl_s
        event_id = ""
        with self._lock, self._db:
            consent = self._db.execute(
                "SELECT * FROM location_consent WHERE account_id=? AND enabled=1",
                (account.id,)).fetchone()
            if consent is None:
                raise BoardError(403, "location_consent_required")
            previous = self._db.execute(
                "SELECT * FROM location_sample WHERE account_id=? AND device_id=?",
                (account.id, device)).fetchone()
            self._db.execute("""
              INSERT INTO location_sample(account_id,device_id,lat,lon,accuracy_m,
                                          observed_at,expires_at,source)
              VALUES (?,?,?,?,?,?,?,?)
              ON CONFLICT(account_id,device_id) DO UPDATE SET lat=excluded.lat,
                lon=excluded.lon,accuracy_m=excluded.accuracy_m,
                observed_at=excluded.observed_at,expires_at=excluded.expires_at,
                source=excluded.source
            """, (account.id, device, lat, lon, accuracy, now, expires, source))
            row = self._db.execute(
                "SELECT * FROM location_sample WHERE account_id=? AND device_id=?",
                (account.id, device)).fetchone()
            location = _pack(row, account)
            changed = previous is None or float(previous["expires_at"]) <= now \
                or float(previous["lat"]) != lat or float(previous["lon"]) != lon
            if changed:
                # 事件日志永久保留，不能成为绕过 TTL／撤销的另一份坐标数据库。
                # Agent 只把更新事件当感知信号；实际天气查询再通过 MCP 读取限时样本。
                notice = {key: value for key, value in location.items()
                          if key not in {"lat", "lon", "accuracy"}}
                event_id = append_event(
                    self._db, event_type="location.updated", at=now,
                    actor_id=account.id, actor_kind=account.kind,
                    entity_kind="device", entity_id=device,
                    payload={"location": notice},
                    audience=[account.id, *self._grants(consent)])
        return {"ok": True, "location": location, "event_id": event_id,
                "changed": bool(changed)}

    def latest_for_agent(self, actor: Actor, *, principal: str = "") -> dict:
        account = self._account(actor)
        if account.kind != "entity":
            raise BoardError(403, "location_entity_only")
        now = float(self.now())
        who = str(principal or "")
        with self._lock, self._db:
            self._db.execute("DELETE FROM location_sample WHERE expires_at<=?", (now,))
            rows = self._db.execute("""
              SELECT s.* FROM location_sample AS s
              JOIN location_consent AS c ON c.account_id=s.account_id AND c.enabled=1
              WHERE s.expires_at>? ORDER BY s.observed_at DESC
            """, (now,)).fetchall()
            grants = {row["account_id"]: self._grants(row) for row in self._db.execute(
                "SELECT account_id,authorized_accounts FROM location_consent WHERE enabled=1")}
        for row in rows:
            owner = self.accounts.get(row["account_id"])
            if owner is None or account.id not in grants.get(owner.id, []):
                continue
            if who and who not in {owner.id, owner.legacy_principal}:
                continue
            return {"location": _pack(row, owner)}
        return {"location": None}


__all__ = ["LocationService", "PRECISION_M", "PURPOSE", "TTL_S"]
