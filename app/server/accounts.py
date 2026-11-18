"""App Server 平台服务账号：文件只存 token 哈希，认证结果映射为 Actor。"""

from __future__ import annotations

import hashlib
import hmac
import json
import pathlib
import re
from dataclasses import dataclass

from app.server.social.models import Actor

_ACCOUNT_ID = re.compile(r"[a-z0-9_-]{1,64}")
_HASH = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    kind: str
    display: str
    token_sha256: str
    visible_entities: frozenset[str] | None = None
    legacy_principal: str = ""

    def actor(self) -> Actor:
        return Actor(self.id, self.kind, self.display, self.visible_entities)

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "display": self.display,
                "visible_entities": sorted(self.visible_entities)
                if self.visible_entities is not None else None}


class AccountStore:
    def __init__(self, path, *, introspector=None) -> None:
        self.path = pathlib.Path(path)
        #: 人類帳號的 token 以 Agent 為準（多設備配對：新端末、撤銷都即時；`device_auth.py`）
        self.introspector = introspector
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        rows = payload.get("accounts") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("accounts file requires an accounts array")
        migration_rows = payload.get("migration_accounts", [])
        if not isinstance(migration_rows, list):
            raise ValueError("migration_accounts must be an array")
        accounts = [self._parse(row) for row in rows]
        migration_accounts = [self._parse(row) for row in migration_rows]
        ids = [account.id for account in accounts]
        migration_ids = [account.id for account in migration_accounts]
        if len(ids) != len(set(ids)) or len(migration_ids) != len(set(migration_ids)):
            raise ValueError("duplicate account id")
        if set(ids) & set(migration_ids):
            raise ValueError("migration account collides with an active account")
        if any(account.kind == "owner" for account in migration_accounts):
            raise ValueError("migration accounts cannot authenticate or administer")
        if sum(account.kind == "owner" for account in accounts) > 1:
            raise ValueError("master must be the only platform administrator")
        self._accounts = tuple(accounts)
        # 仅用于历史归属；认证与会话 ACL 永远只遍历 _accounts。
        self._migration_accounts = tuple(migration_accounts)

    @staticmethod
    def _parse(row) -> Account:
        if not isinstance(row, dict):
            raise ValueError("account must be an object")
        identity = str(row.get("id") or "")
        kind = str(row.get("kind") or "")
        token_hash = str(row.get("token_sha256") or "").lower()
        if not _ACCOUNT_ID.fullmatch(identity):
            raise ValueError(f"invalid account id: {identity!r}")
        if kind not in {"owner", "entity", "guest"}:
            raise ValueError(f"invalid account kind: {kind!r}")
        if not _HASH.fullmatch(token_hash):
            raise ValueError(f"invalid token hash for account: {identity}")
        visible = row.get("visible_entities")
        if visible is not None and (not isinstance(visible, list)
                                    or not all(_ACCOUNT_ID.fullmatch(str(item)) for item in visible)):
            raise ValueError(f"invalid visible_entities for account: {identity}")
        return Account(identity, kind, str(row.get("display") or identity), token_hash,
                       frozenset(str(item) for item in visible) if visible is not None else None,
                       str(row.get("legacy_principal") or ""))

    def get(self, account_id: str) -> Account | None:
        for account in self._accounts:
            if account.id == str(account_id):
                return account
        return None

    def all(self) -> tuple[Account, ...]:
        """当前可认证账号；历史归属账号不得被加入新会话或事件可见域。"""
        return self._accounts

    def resolve_legacy(self, identity: str) -> Account | None:
        """稳定 ID 优先；旧 principal 仅唯一匹配可用，不按昵称猜测。"""
        identity = str(identity or "")
        direct = self.get(identity)
        if direct is not None:
            return direct
        direct_migration = next((account for account in self._migration_accounts
                                 if account.id == identity), None)
        if direct_migration is not None:
            return direct_migration
        matched = [account for account in self._accounts + self._migration_accounts
                   if account.legacy_principal and account.legacy_principal == identity]
        return matched[0] if len(matched) == 1 else None

    def authenticate(self, authorization: str) -> Account | None:
        scheme, _, token = str(authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token or token != token.strip():
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        matched = None
        for account in self._accounts:
            if hmac.compare_digest(digest, account.token_sha256):
                matched = account
        if matched is not None and matched.kind == "entity":
            return matched                     # 角色帳號的 token 只在平台這邊
        introspector = self.introspector
        if introspector is None:
            return matched
        from app.server.device_auth import UNREACHABLE
        answer = introspector.account_for(digest)
        if answer is UNREACHABLE:
            return matched                     # introspection 沒開（沒設 kick token）：只看靜態表
        found = self.get(answer) if answer else None
        return found if found is not None and found.kind != "entity" else None


__all__ = ["Account", "AccountStore"]
