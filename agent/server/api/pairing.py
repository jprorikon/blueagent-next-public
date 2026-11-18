"""多設備配對與端末管理（owner 2026-11-16：二維碼＋6 位數字都要；客人也可以多設備）。

  POST /api/pair/start      已登入（主人或客人）⇒ `{code, expires_at, url, qr}`（`qr`＝PNG data URI，算不出來時為空）
  POST /api/pair/claim      不用登入；`{code, name}` ⇒ `{token, account, device}`（token 只出現這一次）
  GET  /api/devices         自己這個帳號的端末清單（`current` 標出發請求的那一台）
  POST /api/devices/revoke  `{device_id}` 撤銷自己帳號的一台（撤掉自己這台＝登出這台）
  GET  /pair/<碼>           系統相機掃二維碼開到的落地頁：一顆鈕跳回 App（`blueagent://pair?code=`）
  POST /api/app/introspect  App Server 問「這把 token（雜湊）是誰的哪一台」（`X-App-Kick-Token`）

服務帳號（bot）不能配對、也沒有端末清單。
"""

from __future__ import annotations

import html
import re

from agent.brain.compose.state import AppState
from agent.brain.domain.auth import AuthContext, Role
from agent.server.api.common import _forbidden, _unauthorized

#: 猜碼節流：每個 IP 每分鐘最多試幾次（6 位數＝一百萬種，5 分鐘內猜中機率 ≈ 50／1e6）
CLAIM_PER_MINUTE = 10
_BASE = re.compile(r"^https?://[A-Za-z0-9.\-:\[\]]+(/[A-Za-z0-9._~\-/]*)?$")


def _human(auth: AuthContext | None) -> tuple[int, dict] | None:
    if auth is None:
        return _unauthorized()
    if auth.role is Role.SERVICE or not auth.token_id:
        return _forbidden()
    return None


def _qr_png(text: str) -> str:
    """二維碼 PNG（data URI）；image 裡沒裝 `segno` 就回空字串（端末照樣顯示 6 位數字）。"""
    try:
        import segno
    except ImportError:
        return ""
    try:
        return segno.make(text, error="m").png_data_uri(scale=8, border=4)
    except Exception:                                   # noqa: BLE001 - 二維碼壞了不擋配對碼
        return ""


def post_pair_start(state: AppState, body: dict, *, auth: AuthContext | None = None) -> tuple[int, dict]:
    denied = _human(auth)
    if denied is not None:
        return denied
    started = state.app.auth.pairing.start(auth.token_id)
    base = str((body or {}).get("base_url") or "").strip().rstrip("/")
    url = f"{base}/pair/{started['code']}" if _BASE.match(base) else ""
    state.app.auth.audit("pair.start", auth=auth, path="/api/pair/start")
    return 200, {"code": started["code"], "expires_at": started["expires_at"],
                 "url": url, "qr": _qr_png(url) if url else ""}


def post_pair_claim(state: AppState, body: dict, *, ip: str = "") -> tuple[int, dict]:
    auth_service = state.app.auth
    if not auth_service.allow(f"pair:{ip or 'local'}", limit=CLAIM_PER_MINUTE, window_s=60.0):
        return 429, {"error": "rate_limited", "retry_after_s": 60}
    account_id = auth_service.pairing.claim(str((body or {}).get("code") or ""))
    if not account_id:
        auth_service.audit("pair.claim", path="/api/pair/claim", ip=ip, ok=False, code="bad_code")
        return 404, {"error": "bad_or_expired_code"}
    accounts = state.app.accounts
    try:
        device, token = accounts.add_device(account_id, str((body or {}).get("name") or ""))
    except KeyError:
        return 404, {"error": "bad_or_expired_code"}
    except ValueError as exc:
        return 409, {"error": str(exc)}
    auth_service.audit("pair.claim", path="/api/pair/claim", ip=ip, ok=True)
    row = accounts.get(account_id) or {}
    account = {key: row.get(key, "") for key in ("account_id", "role", "principal", "display")}
    return 200, {"token": token, "account": account,
                 "device": {key: device[key] for key in ("device_id", "name", "created_at")}}


def get_devices(state: AppState, *, auth: AuthContext | None = None) -> tuple[int, dict]:
    denied = _human(auth)
    if denied is not None:
        return denied
    return 200, {"devices": state.app.accounts.devices(auth.token_id, current=auth.device)}


def post_device_revoke(state: AppState, body: dict, *,
                       auth: AuthContext | None = None) -> tuple[int, dict]:
    denied = _human(auth)
    if denied is not None:
        return denied
    device_id = str((body or {}).get("device_id") or "")
    if not state.app.accounts.revoke_device(auth.token_id, device_id):
        return 404, {"error": "unknown_device"}
    state.app.auth.audit("device.revoke", auth=auth, path="/api/devices/revoke")
    return 200, {"ok": True, "device_id": device_id, "self": device_id == auth.device}


def introspect(state: AppState, body: dict) -> tuple[int, dict]:
    """App Server 用（已驗過 kick token）：雜湊 ⇒ App 帳號 id。服務帳號在 App Server 不存在 ⇒ 一律無效。"""
    accounts = getattr(state.app, "accounts", None)
    row = accounts.verify_hash(str((body or {}).get("token_sha256") or "")) if accounts else None
    if row is None or str(row.get("role") or "") == "service":
        return 200, {"valid": False}
    app_ids = accounts.links(str(row.get("account_id") or "")).get("app") or []
    return 200, {"valid": True, "account_id": str(app_ids[0] if app_ids else row.get("account_id")),
                 "device": str(row.get("device_id") or "")}


PAGE = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>BlueAgent 端末の追加</title>
<style>body{{font-family:system-ui,sans-serif;background:#f4f8fc;color:#173558;margin:0;padding:32px 20px;text-align:center}}
.code{{font-size:44px;letter-spacing:8px;font-weight:700;margin:20px 0}}
a.btn{{display:inline-block;background:#198ac4;color:#fff;padding:14px 28px;border-radius:24px;text-decoration:none;font-size:18px}}
p{{line-height:1.6}}</style></head><body>
<h1>端末の追加</h1>{body}</body></html>"""


def pair_page(state: AppState, code: str) -> tuple[int, str]:
    """落地頁（不用登入；不消耗碼——真正的兌換由 App 送 `/api/pair/claim`）。"""
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())[:6]
    if len(digits) != 6 or not state.app.auth.pairing.pending(digits):
        return 404, PAGE.format(body="<p>このコードは無効か、期限が切れています。<br>"
                                     "もとの端末でもう一度「端末を追加」を押してください。</p>")
    safe = html.escape(digits)
    return 200, PAGE.format(body=(
        f'<div class="code">{safe}</div>'
        f'<p><a class="btn" href="intent://pair?code={safe}#Intent;scheme=blueagent;end">'
        "BlueAgent で開く</a></p>"
        f'<p>開かない場合は <a href="blueagent://pair?code={safe}">こちら</a>、'
        "またはアプリのログイン画面で上の6桁を入力してください（5分間有効）。</p>"))


__all__ = ["get_devices", "introspect", "pair_page", "post_device_revoke", "post_pair_claim",
           "post_pair_start"]
