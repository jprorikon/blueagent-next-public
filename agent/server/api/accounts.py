"""帳號與身分：/api/me、/api/accounts*。"""

from __future__ import annotations

import time

from agent.brain.compose.state import AppState
from agent.brain.domain.auth import AuthContext

from agent.server.api.common import _owner_required, _unauthorized


def get_me(state: AppState, *, auth: AuthContext | None = None) -> tuple[int, dict]:
    """`GET /api/me`：回身份、能力與可見角色，不含 token／雜湊。"""
    if auth is None:
        return _unauthorized()
    accounts = getattr(state.app, "accounts", None)
    row = state.app.auth.account_for(auth) if accounts is not None else None
    role = "master" if auth.is_owner else "guest"
    capabilities = sorted(row.get("capabilities") or ()) if row else sorted(auth.capabilities)
    entities = []
    try:
        found = state.app.entities.all()
        if auth.is_owner:
            entities = [{"id": item.id, "name": item.name} for item in found]
        else:
            # guest 也能選說話對象（owner 2026-10-29「guest 主頁不能選擇說話對象」）：給全部角色；
            # 只有「綁定在某個角色上」的 guest 帳號（principal 就是那個角色 id）仍只看那一位。
            # 送話本來就收 `entity`（`prepare_send` 不按身分限角色），所以這裡是唯一要開的地方。
            bound = [item for item in found if item.id == auth.principal]
            picked = bound or list(found)
            entities = [{"id": item.id, "name": item.name} for item in picked]
    except Exception:  # noqa: BLE001
        entities = []
    return 200, {
        "account_id": str((row or {}).get("account_id") or auth.token_id or ""),
        "role": role, "principal": auth.principal,
        "display": str((row or {}).get("display") or auth.principal),
        "capabilities": capabilities, "entities": entities,
        "can": {"chat": True, "voice": True,
                # 讓她進 TRPG 桌只給主人（owner 2026-10-29；`server/api/games.py` 擋 403）
                "trpg": bool(auth.is_owner), "admin": bool(auth.is_owner),
                "manage_accounts": bool(auth.is_owner), "manage_guests": bool(auth.is_owner),
                "delegate": bool(auth.is_owner)},
        "created_at": float((row or {}).get("created_at") or 0.0),
        # 這是帳號的哪一台端末（多設備配對，2026-11-16）：設定 › 端末 標「この端末」用
        "device": auth.device,
    }


def list_accounts(state: AppState, *, auth: AuthContext | None = None) -> tuple[int, dict]:
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    if not state.account_service.available:
        return 503, {"error": "accounts_unavailable"}
    return 200, state.account_service.list()


def export_accounts(state: AppState, *, auth: AuthContext | None = None) -> tuple[int, dict]:
    """`GET /api/accounts/export`：匯出帳號層（換機／遷移用；owner 2026-10-31 第 3 項）。

    含 token **雜湊**（不是明文）——換機時能讓端末上已經貼好的 token 繼續有效。
    仍然算敏感資料（雜湊可被離線暴力破解弱 token，我們的 token 是 32+ 字元隨機），
    所以：只有主人能拿、只有主人能匯入。
    """
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    store = getattr(state.app, "accounts", None)
    if store is None:
        return 503, {"error": "accounts_unavailable"}
    return 200, {"format": "blueagent-accounts/1", "exported_at": time.time(),
                 "accounts": store.export_rows(),
                 "warning": "含 token 雜湊（不含明文）：等同鑰匙，別貼群組、別進版控。"}


def import_accounts(state: AppState, body: dict, *,
                    auth: AuthContext | None = None) -> tuple[int, dict]:
    """`POST /api/accounts/import`：匯入（upsert，不刪任何現有帳號）。"""
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    store = getattr(state.app, "accounts", None)
    if store is None:
        return 503, {"error": "accounts_unavailable"}
    rows = body.get("accounts") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return 400, {"error": "accounts_required"}
    try:
        result = store.merge_rows(rows)
    except ValueError as exc:
        return 400, {"error": "bad_payload", "detail": str(exc)}
    state.app.auth.audit("account.import", auth=auth, path="/api/accounts/import")
    master_hash = str(result.pop("master_hash", "") or "")
    out = {"ok": True, **result, "total": len(store.list(include_revoked=True)),
           "master_replaced": bool(master_hash)}
    if master_hash:
        # 主人的 token 換成匯入檔裡那一個：auth.json 也要換，不然重啟被蓋回去；
        # 而且**現在發這個請求用的 token 立刻失效**——要明講（2026-10-31 複檢）
        try:
            state.app.auth.adopt_owner_hash(master_hash)
        except Exception as exc:                            # noqa: BLE001
            print(f"accounts: 匯入的主人雜湊寫不進 auth.json（{type(exc).__name__}: {exc}）", flush=True)
        out["warning"] = ("主人 token 已換成匯入檔裡的那一個：發這個請求用的 token 已失效，"
                          "請用舊機器上的主人 token 登入。")
    return 200, out


#: 能簽服務帳號的源（目前只有 Discord）
SERVICE_SOURCES = frozenset({"discord"})


def create_account(state: AppState, body: dict, *,
                   auth: AuthContext | None = None) -> tuple[int, dict]:
    """簽一個 guest 帳號（主人用）。明文 token 只回這一次。"""
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    if not state.account_service.available:
        return 503, {"error": "accounts_unavailable"}
    role = str(body.get("role") or state.account_service.role_guest).strip().lower()
    principal = str(body.get("principal") or "").strip()
    if role == "service":
        # Discord 源級認證（owner 2026-11-16「每個 bot 一個 token」）：`{"role":"service","source":"discord","entity":"aster"}`
        source = str(body.get("source") or "").strip().lower()
        entity = str(body.get("entity") or "").strip().lower()
        if source not in SERVICE_SOURCES or not entity:
            return 400, {"error": "service_needs_source_and_entity", "sources": sorted(SERVICE_SOURCES)}
        row, token = state.account_service.issue_service(source=source, entity=entity)
        state.app.auth.audit("account.create_service", auth=auth, path="/api/accounts")
        return 201, {"account": row, "token": token,
                     "hint": "這個 token 只顯示一次；設定到那顆 bot 的 Authorization: Bearer。"}
    if role != state.account_service.role_guest:
        return 400, {"error": "only_guest_role_can_be_issued"}
    if not principal:
        return 400, {"error": "principal_required"}
    row, token = state.account_service.issue(
        role=role, principal=principal, display=str(body.get("display") or principal),
        capabilities=body.get("capabilities") or None)
    # 單一真源（owner 2026-10-31）：簽發的 guest 也要留在 `guests.json`。
    # 啟動時的遷移只認那份 approved 名單；只寫 `accounts.json` 的話，那個檔一壞／被重建，
    # 這個帳號就永久消失（回報症狀：重建容器後只剩 owner 與 guest-707）。
    guests = getattr(state.app.auth, "guests", None)
    if guests is not None:
        try:
            guests.remember_account(
                principal_id=principal, token=token,
                display_name=str(body.get("display") or principal),
                capabilities=tuple(row.get("capabilities") or ("chat",)))
        except Exception as exc:                        # noqa: BLE001 — 記不進去不擋簽發，但要吵
            print(f"accounts: 記進 guests.json 失敗（{type(exc).__name__}: {exc}）"
                  f"⇒ 這個 guest 在重建容器後可能消失", flush=True)
    state.app.auth.audit("account.create", auth=auth, path="/api/accounts")
    return 201, {"account": row, "token": token,
                 "hint": "這個 token 只顯示一次；貼進 guest 端的設定頁。"}


def rotate_account(state: AppState, account_id: str, *,
                   auth: AuthContext | None = None) -> tuple[int, dict]:
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    if not state.account_service.available:
        return 503, {"error": "accounts_unavailable"}
    result = state.account_service.rotate(account_id)
    if result is None:
        return 404, {"error": "account_not_found"}
    row, token = result
    # 單一真源：輪替也要同步到 `guests.json`，否則舊 token 會從 legacy 那條路繼續有效
    # （`AuthService.authenticate` 在帳號層查不到時會退回 `guests.by_token`）。
    # ⚠️ **只對 guest**：主人輪替自己時寫進去 ⇒ guests.json 多一個「owner」guest（還是明文 token），
    #    下次啟動 `restore_guests` 建 owner→owner 關係直接拋例外、整個服務起不來（2026-10-31 複檢實測）
    principal = str((row or {}).get("principal") or "")
    guests = getattr(state.app.auth, "guests", None)
    if principal and guests is not None and str((row or {}).get("role") or "") == "guest":
        try:
            guests.remember_account(principal_id=principal, token=token,
                                    capabilities=tuple((row or {}).get("capabilities") or ("chat",)))
        except Exception as exc:                        # noqa: BLE001
            print(f"accounts: 輪替同步 guests.json 失敗（{type(exc).__name__}: {exc}）", flush=True)
    state.app.auth.audit("account.rotate", auth=auth, path=f"/api/accounts/{account_id}")
    return 200, {"account": row, "token": token}


def revoke_account(state: AppState, account_id: str, *,
                   auth: AuthContext | None = None) -> tuple[int, dict]:
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    if not state.account_service.available:
        return 503, {"error": "accounts_unavailable"}
    row = None
    store = getattr(state.app, "accounts", None)
    try:
        row = store.get(account_id) if store is not None else None
    except Exception:                                  # noqa: BLE001
        row = None
    if not state.account_service.revoke(account_id):
        return 404, {"error": "account_not_found"}
    # 單一真源：撤銷也要撤到 `guests.json`，否則下次啟動的遷移會把它當「已核可」重建回來（只對 guest）
    principal = str((row or {}).get("principal") or "")
    guests = getattr(state.app.auth, "guests", None)
    if principal and guests is not None and str((row or {}).get("role") or "") == "guest":
        try:
            guests.revoke(principal)
        except Exception as exc:                        # noqa: BLE001
            print(f"accounts: 撤銷 guest 名單失敗（{type(exc).__name__}: {exc}）"
                  f"⇒ {principal} 可能在下次啟動被重建", flush=True)
    state.app.auth.audit("account.revoke", auth=auth, path=f"/api/accounts/{account_id}")
    return 200, {"ok": True, "account_id": account_id}
