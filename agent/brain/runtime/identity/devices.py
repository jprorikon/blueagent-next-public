"""一個帳號、多台端末（owner 2026-11-16 拍板「多設備配對；客人也可以多設備」）。

為什麼要這一層：以前一個帳號只有一把 token（`token_sha256`），主人的每台設備都貼同一把——
換新手機只能手抄；輪替＝全部設備一起掉線；丟了一台也沒辦法只踢它。

現在帳號那一列多一個 `devices` 陣列，每台端末一把自己的 token（只存雜湊）：
  · 原本那把 `token_sha256` 視為「第一台」（`device_id="primary"`），既有端末不受影響；
  · 新端末由**配對**簽出（`identity/pairing.py`），帶名字、建立／最後使用時間；
  · 撤銷只動那一台（`revoked_at`）；撤銷 primary 寫在 `token_revoked_at`（輪替時清掉）。

這裡只有對「一列帳號 dict」的純函式；鎖、存檔、找列由 `AccountStore` 負責。
"""

from __future__ import annotations

import secrets

PRIMARY = "primary"
#: 一個帳號最多幾台（含 primary）：配對碼外洩時能簽出去的上限
MAX_DEVICES = 12


def match(row: dict, digest: str) -> str:
    """這把 token（雜湊）是不是這個帳號的哪一台；回 device_id，不是回空字串。"""
    if not digest:
        return ""
    primary = str(row.get("token_sha256") or "")
    if primary and not row.get("token_revoked_at") and secrets.compare_digest(primary, digest):
        return PRIMARY
    for device in row.get("devices") or ():
        if not isinstance(device, dict) or device.get("revoked_at"):
            continue
        if secrets.compare_digest(str(device.get("token_sha256") or ""), digest):
            return str(device.get("device_id") or "")
    return ""


def active_count(row: dict) -> int:
    live = sum(1 for device in row.get("devices") or ()
               if isinstance(device, dict) and not device.get("revoked_at"))
    return live + (0 if row.get("token_revoked_at") else 1)


def add(row: dict, *, name: str, token_hash: str, now: float) -> dict:
    """幫這一列加一台端末；超過上限丟 `ValueError("too_many_devices")`。"""
    if active_count(row) >= MAX_DEVICES:
        raise ValueError("too_many_devices")
    devices = [item for item in row.get("devices") or () if isinstance(item, dict)]
    taken = {str(item.get("device_id") or "") for item in devices}
    ident = ""
    for _ in range(20):
        ident = "dev_" + secrets.token_hex(4)
        if ident not in taken:
            break
    device = {"device_id": ident, "name": str(name or "").strip()[:40] or "端末",
              "token_sha256": str(token_hash), "created_at": round(float(now), 3),
              "last_seen_at": 0.0, "revoked_at": 0.0}
    row["devices"] = devices + [device]
    return dict(device)


def touch(row: dict, device_id: str, now: float) -> None:
    if device_id == PRIMARY:
        row["last_seen_at"] = row["primary_seen_at"] = round(float(now), 3)
        return
    for device in row.get("devices") or ():
        if isinstance(device, dict) and device.get("device_id") == device_id:
            device["last_seen_at"] = round(float(now), 3)
            row["last_seen_at"] = round(float(now), 3)
            return


def revoke(row: dict, device_id: str, now: float) -> bool:
    if device_id == PRIMARY:
        if row.get("token_revoked_at"):
            return False
        row["token_revoked_at"] = round(float(now), 3)
        return True
    for device in row.get("devices") or ():
        if isinstance(device, dict) and device.get("device_id") == device_id \
                and not device.get("revoked_at"):
            device["revoked_at"] = round(float(now), 3)
            return True
    return False


def public(row: dict, *, current: str = "") -> list[dict]:
    """給端末看的設備清單（不含雜湊）；`current`＝發請求的這一台。"""
    first = {"device_id": PRIMARY, "name": str(row.get("primary_name") or "最初の端末"),
             "created_at": float(row.get("created_at") or 0.0),
             "last_seen_at": float(row.get("primary_seen_at") or row.get("last_seen_at") or 0.0),
             "revoked": bool(row.get("token_revoked_at"))}
    rows = [first] + [{"device_id": str(item.get("device_id") or ""),
                       "name": str(item.get("name") or ""),
                       "created_at": float(item.get("created_at") or 0.0),
                       "last_seen_at": float(item.get("last_seen_at") or 0.0),
                       "revoked": bool(item.get("revoked_at"))}
                      for item in row.get("devices") or () if isinstance(item, dict)]
    for item in rows:
        item["current"] = item["device_id"] == current
    return rows


__all__ = ["MAX_DEVICES", "PRIMARY", "active_count", "add", "match", "public", "revoke", "touch"]
