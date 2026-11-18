"""Guest 加入申請、關係聲明、主人審核、Discord 白名單的持久化。"""

from __future__ import annotations

import json
import pathlib
import re
import secrets
import threading
import time

from agent.brain.domain.guest import (GuestRelationProposal, GuestRequest,
                                         RelationDeclaration, RequestStatus, hash_guest_token)
from agent.brain.domain.ids import norm

_KEEP = re.compile(r"[^a-z0-9]+")


class GuestStore:
    def __init__(self, path=None, *, default_invite: str = "blueagent") -> None:
        self._path = pathlib.Path(path) if path else None
        self._lock = threading.RLock()
        self._requests: dict[str, GuestRequest] = {}
        self._invites: dict[str, int] = {}
        if self._path is not None:
            self._load()
        if default_invite and default_invite not in self._invites:
            self._invites[default_invite] = -1          # Demo 預設不限次
        self._reconcile_pairs_locked()
        self._save_locked()

    # ── 邀請碼 ──────────────────────────────────────────────────────────────────
    def create_invite(self, code: str = "", uses: int = 1) -> dict:
        token = code or f"inv_{secrets.token_hex(4)}"
        with self._lock:
            self._invites[token] = int(uses)
            self._save_locked()
        return {"code": token, "uses": int(uses)}

    def invites(self) -> dict:
        return dict(self._invites)

    def has_invite(self, code: str) -> bool:
        with self._lock:
            return str(code or "").strip() in self._invites

    # ── 加入申請 ────────────────────────────────────────────────────────────────
    def create_request(self, *, invite_code: str, discord_id: str,
                       display_name: str, declared: RelationDeclaration,
                       now: float | None = None,
                       guest_relations=()) -> GuestRequest:
        code = str(invite_code or "").strip()
        discord = str(discord_id or "").strip()
        if not discord:
            raise ValueError("discord_id required")
        with self._lock:
            uses = self._invites.get(code)
            if uses is None:
                raise ValueError("invalid invite code")
            for request in self._requests.values():
                if request.discord_id == discord and request.status is RequestStatus.PENDING:
                    return request
            if uses > 0:
                self._invites[code] = uses - 1
            relations = self._resolve_relations(guest_relations, exclude_discord=discord)
            request = GuestRequest.create(code, discord, display_name, declared, now=now,
                                          guest_relations=relations)
            self._requests[request.id] = request
            self._save_locked()
            return request

    def get(self, request_id: str) -> GuestRequest | None:
        with self._lock:
            return self._requests.get(str(request_id))

    def by_secret(self, secret: str) -> GuestRequest | None:
        with self._lock:
            for request in self._requests.values():
                if request.request_secret == str(secret):
                    return request
        return None

    def pending(self) -> list[GuestRequest]:
        with self._lock:
            return [request for request in self._requests.values()
                    if request.status is RequestStatus.PENDING]

    def approve(self, request_id: str, *, final: RelationDeclaration,
                trust: float = 0.5, capabilities=("chat",),
                decided_by: str = "owner", principal_id: str = "",
                final_guests=()) -> GuestRequest:
        with self._lock:
            request = self._requests.get(str(request_id))
            if request is None:
                raise KeyError(f"unknown request: {request_id}")
            if request.status is not RequestStatus.PENDING:
                raise ValueError(f"request is {request.status.value}")
            pid = norm(principal_id or self._unique_principal_id(request.discord_id),
                       fallback="guest")
            request.status = RequestStatus.APPROVED
            request.final = final
            request.trust = float(trust)
            request.capabilities = tuple(str(cap) for cap in capabilities)
            request.decided_at = time.time()
            request.decided_by = str(decided_by or "owner")
            request.principal_id = pid
            request.set_token(f"gt_{secrets.token_urlsafe(24)}")     # 明文等 guest 來拿一次，之後只留雜湊
            finals = self._resolve_relations(final_guests, exclude_discord=request.discord_id)
            request.final_guests = finals or request.declared_guests
            self._save_locked()
            return request

    def reject(self, request_id: str, *, decided_by: str = "owner") -> GuestRequest:
        with self._lock:
            request = self._requests.get(str(request_id))
            if request is None:
                raise KeyError(f"unknown request: {request_id}")
            if request.status is not RequestStatus.PENDING:
                raise ValueError(f"request is {request.status.value}")
            request.status = RequestStatus.REJECTED
            request.decided_at = time.time()
            request.decided_by = str(decided_by or "owner")
            self._save_locked()
            return request

    def revoke(self, principal_id: str) -> bool:
        with self._lock:
            for request in self._requests.values():
                if request.principal_id == principal_id and request.status is RequestStatus.APPROVED:
                    request.status = RequestStatus.REVOKED
                    request.decided_at = time.time()
                    request.clear_token()
                    self._save_locked()
                    return True
        return False

    # ── 查詢 ────────────────────────────────────────────────────────────────────
    def accounts(self) -> list[GuestRequest]:
        with self._lock:
            return [request for request in self._requests.values()
                    if request.status is RequestStatus.APPROVED]

    def remember_account(self, *, principal_id: str, token: str, display_name: str = "",
                         capabilities=("chat",), discord_id: str = "") -> GuestRequest:
        """把「帳號層簽發的 guest」也記進 `guests.json`（owner 2026-10-31）。

        為什麼要重複記一份：App 建的 guest 原本**只**活在 `accounts.json`，而啟動時的
        `AuthService._migrate_legacy` 只認 `guests.json` 的 approved 名單 ⇒ 那個檔一壞、
        或被當成空庫重建，帳號就永久消失（回報症狀：重建容器後只剩 owner 與 guest-707）。

        記進來之後 `guests.json` 是「誰被核可過」的權威，`accounts.json` 只是它的憑證索引，
        每次啟動都能重建回來。同一個 principal 已存在就更新（不長第二筆）。
        """
        pid = str(principal_id or "").strip()
        if not pid:
            raise ValueError("principal_id required")
        with self._lock:
            for request in self._requests.values():
                if request.principal_id == pid and request.status is RequestStatus.APPROVED:
                    if token:
                        request.remember_hash(token)
                    request.display_name = str(display_name or request.display_name)
                    request.capabilities = tuple(capabilities or request.capabilities)
                    self._save_locked()
                    return request
            request = GuestRequest.create(
                invite_code="account",
                discord_id=str(discord_id or f"acct:{pid}"),
                display_name=str(display_name or pid),
                declared=RelationDeclaration(kind="friend", label="朋友", note="帳號層簽發"))
            request.status = RequestStatus.APPROVED
            request.decided_at = time.time()
            request.decided_by = "owner"
            request.final = request.declared
            request.trust = 0.5
            request.capabilities = tuple(capabilities or ("chat",))
            request.principal_id = pid
            request.remember_hash(token)
            self._requests[request.id] = request
            self._save_locked()
            return request

    def by_discord_id(self, discord_id: str) -> GuestRequest | None:
        """**已批准**的 guest（按 Discord id）。用於「白名單」判定：
        沒批准過的人不是 guest，只是陌生人——不該拿到任何回覆。"""
        wanted = str(discord_id or "").strip()
        if not wanted:
            return None
        with self._lock:
            for request in self._requests.values():
                if (request.discord_id == wanted
                        and request.status is RequestStatus.APPROVED):
                    return request
        return None

    def by_token(self, token: str) -> GuestRequest | None:
        if not token:
            return None
        digest = hash_guest_token(token)
        with self._lock:
            for request in self._requests.values():
                if request.status is RequestStatus.APPROVED and request.token_sha256 \
                        and secrets.compare_digest(request.token_sha256, digest):
                    return request
        return None

    def deliver_token(self, request: GuestRequest) -> None:
        """guest 拿到明文了（`/api/guest/status` 回過一次）⇒ 明文清掉，只留雜湊。"""
        with self._lock:
            if request.token:
                request.token = ""
                self._save_locked()

    def add_pair(self, left_principal: str, right_principal: str, *,
                 kind: str = "acquaintance", label: str = "", note: str = "") -> None:
        """批准 guest×guest 關係時，把邊補到雙方的 final_guests（對稱）。"""
        left_id = norm(left_principal, fallback="")
        right_id = norm(right_principal, fallback="")
        with self._lock:
            left = self._by_principal(left_id)
            right = self._by_principal(right_id)
            if left is None or right is None:
                return
            left.final_guests = self._upsert_pairs(
                left.final_guests, right_id, kind, label, note)
            right.final_guests = self._upsert_pairs(
                right.final_guests, left_id, kind, label, note)
            self._save_locked()

    def _by_principal(self, principal_id: str) -> GuestRequest | None:
        for request in self.accounts():
            if request.principal_id == principal_id:
                return request
        return None

    @staticmethod
    def _upsert_pairs(items, other_principal: str, kind: str, label: str,
                      note: str) -> tuple[GuestRelationProposal, ...]:
        rows = [item for item in items if item.other_principal != other_principal]
        rows.append(GuestRelationProposal(other_discord_id="",
                                          other_principal=other_principal,
                                          kind=kind, label=label, note=note))
        return tuple(rows)

    def directory(self) -> list[dict]:
        return [{"principal": request.principal_id,
                 "display_name": request.display_name,
                 "discord_id": request.discord_id,
                 "relation": request.final.to_dict() if request.final else None}
                for request in self.accounts()]

    def whitelist(self) -> list[dict]:
        return [{"discord_id": request.discord_id,
                 "principal": norm(request.principal_id, fallback="guest"),
                 "display_name": request.display_name,
                 "relation": request.final.to_dict() if request.final else None}
                for request in self.accounts()]

    def snapshot(self) -> dict:
        pending = [request.to_dict() for request in self.pending()]
        accounts = [request.to_dict() for request in self.accounts()]
        return {"pending": pending, "accounts": accounts,
                "invites": self.invites()}

    def _resolve_relations(self, rows, *, exclude_discord: str = ""):
        resolved: list[GuestRelationProposal] = []
        for row in rows or ():
            item = row if isinstance(row, GuestRelationProposal) else (
                GuestRelationProposal.from_dict(row) if isinstance(row, dict) else None)
            if item is None:
                continue
            other = item.other_principal
            if not other and item.other_discord_id:
                other = self._principal_for_discord(item.other_discord_id)
            if not other or other == norm(f"guest-{exclude_discord}", fallback="guest"):
                continue
            resolved.append(GuestRelationProposal(
                other_discord_id=item.other_discord_id, other_principal=other,
                kind=item.kind, label=item.label, note=item.note))
        return tuple(resolved)

    def _principal_for_discord(self, discord_id: str) -> str:
        for request in self.accounts():
            if request.discord_id == str(discord_id):
                return request.principal_id
        return ""

    def _reconcile_pairs_locked(self) -> None:
        for request in self.accounts():
            for relation in list(request.final_guests):
                other = self._by_principal(relation.other_principal)
                if other is None:
                    continue
                if not any(item.other_principal == request.principal_id
                           for item in other.final_guests):
                    other.final_guests = self._upsert_pairs(
                        other.final_guests, request.principal_id, relation.kind,
                        relation.label, relation.note)

    def _unique_principal_id(self, discord_id: str) -> str:
        base = norm(f"guest-{discord_id}", fallback="guest")
        used = {request.principal_id for request in self._requests.values()}
        if base not in used:
            return base
        index = 2
        while f"{base}-{index}" in used:
            index += 1
        return f"{base}-{index}"

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return
        if not isinstance(raw, dict):
            return
        for row in raw.get("requests") or ():
            if isinstance(row, dict):
                request = GuestRequest.from_dict(row)
                if request.principal_id:
                    request.principal_id = norm(request.principal_id, fallback="guest")
                self._requests[request.id] = request
        invites = raw.get("invites")
        if isinstance(invites, dict):
            self._invites = {str(code): int(uses) for code, uses in invites.items()}

    def _save_locked(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"requests": [request.to_dict()
                                                    for request in self._requests.values()],
                                       "invites": self._invites},
                                      ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            pass
