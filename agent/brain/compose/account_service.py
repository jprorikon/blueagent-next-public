"""帳號生命週期的應用服務；HTTP 層不直接依賴存儲實作。"""

from __future__ import annotations

from agent.brain.runtime.identity.accounts import ROLE_GUEST, ROLE_MASTER, public_row


class AccountService:
    role_guest = ROLE_GUEST

    def __init__(self, app) -> None:
        self._app = app

    @property
    def available(self) -> bool:
        return getattr(self._app, "accounts", None) is not None

    def list(self) -> dict:
        store = self._app.accounts
        return {"accounts": [public_row(row) for row in store.list(include_revoked=True)],
                "stats": store.snapshot()}

    def issue(self, *, role: str, principal: str, display: str, capabilities=None) -> tuple[dict, str]:
        row, token = self._app.accounts.issue(role=role, principal=principal, display=display,
                                             capabilities=capabilities)
        return public_row(row), token

    def issue_service(self, *, source: str, entity: str) -> tuple[dict, str]:
        """每顆 bot 一把服務 token（owner 2026-11-16）：principal 固定是 `svc-<源>-<角色>`。"""
        principal = f"svc-{source}-{entity}"
        row, token = self._app.accounts.issue(role="service", principal=principal,
                                             display=f"{source} bot（{entity}）",
                                             source=source, entity=entity)
        return public_row(row), token

    def rotate(self, account_id: str) -> tuple[dict, str] | None:
        store = self._app.accounts
        current = store.get(account_id)
        if current is not None and str(current.get("role") or "") == ROLE_MASTER:
            # 主人 token 的權威是 `data/auth.json`：啟動時 `AuthService._migrate_accounts` 拿它重建 acct_owner。
            # 只換 accounts.json 的話，重啟就被蓋回舊 token、新 token 失效（2026-10-31 複檢）⇒ 走 AuthService.rotate，
            # 它會同時改 auth.json 與帳號層。（`BLUEAGENT_OWNER_TOKEN` 有設時以環境變數為準，輪替擋不住重啟）
            token = self._app.auth.rotate()
            row = store.get("acct_owner") or store.get(account_id)
            return (public_row(row), token) if row is not None else None
        result = store.rotate(account_id)
        if result is None:
            return None
        row, token = result
        return public_row(row), token

    def revoke(self, account_id: str) -> bool:
        return self._app.accounts.revoke(account_id)
