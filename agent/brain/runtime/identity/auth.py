"""Token → AuthContext：owner token、guest token、角色、限流与审计。

这里不信任请求体里的 `principal`。Guest 只能是自己；owner 可以为了调试代某个人说话，
但那是 owner 的显式能力，不是 guest 能自报的。
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import secrets
import threading
import time
from collections import deque

from agent.brain.domain.auth import AuthContext, Role
from agent.brain.domain.ids import norm
from agent.brain.domain.principal import PrincipalRegistry
from agent.brain.runtime.fileperm import adopt_owner
from agent.brain.runtime.identity.guest_store import GuestStore
from agent.brain.runtime.identity.pairing import PairingStore


#: 服務帳號（bot）只能打這幾條：發回合（文字／圖／語音）。其他一律當沒認證（不進管理面、不讀記憶）
SERVICE_PATHS = frozenset({"/api/turn", "/api/stream", "/api/image", "/api/media", "/api/audio"})


class AuthService:
    def __init__(self, path: pathlib.Path | str, principals: PrincipalRegistry,
                 guests: GuestStore, *, owner_principal: str = "",
                 owner_token: str = "", chat_limit: int = 60,
                 join_limit: int = 10, window_s: float = 60.0,
                 accounts=None) -> None:
        self.path = pathlib.Path(path)
        self.principals = principals
        self.guests = guests
        self.owner_principal = norm(
            owner_principal or principals.default(), fallback="owner")
        self.chat_limit = max(1, int(chat_limit))
        self.join_limit = max(1, int(join_limit))
        self.window_s = max(1.0, float(window_s))
        self._lock = threading.RLock()
        self._hits: dict[str, deque[float]] = {}
        self._audit_path = self.path.with_name("auth.log.jsonl")
        env_token = os.environ.get("BLUEAGENT_OWNER_TOKEN") or os.environ.get(
            "OWNER_TOKEN") or ""
        #: 只存 **SHA-256**，不存明文（`data/auth.json` 被看到也不等於拿到 token）。
        #: 舊檔是明文 ⇒ 這裡當場升級（本次啟動仍用原 token，不會把主人鎖在外面）。
        self._token_hash = ""
        self.created_now = False        # 這次啟動才新建的 token（只有這種情況要印出來）
        self._token = str(owner_token or env_token).strip()
        if self._token:
            self._token_hash = self._hash(self._token)
            if not self.path.exists():
                self._save_hash(self._token_hash)
        else:
            loaded_token, legacy = self._load_or_create()
            # 新建或舊檔升級都會給明文（只給這一次）；已存 hash 的情況會是空字串
            if loaded_token:
                self._token = loaded_token
                self._token_hash = self._hash(loaded_token)
                if legacy:
                    self._save_hash(self._token_hash)   # 明文當場換成 hash 寫回
        # 帳號層（owner 2026-10-22）：主人也要有帳號物件，Unity 的 master 端才問得出「我是誰」。
        self.accounts = accounts
        #: 配對碼（多設備，2026-11-16）：只放記憶體，重啟作廢
        self.pairing = PairingStore()
        self._migrate_accounts()

    def _migrate_accounts(self) -> None:
        """把既有的主人 token 收編成一個 master 帳號（只做一次，重啟不會重複）。"""
        store = self.accounts
        if store is None or not self._token_hash:
            return
        try:
            store.import_hash(role="master", principal=self.owner_principal,
                              token_hash=self._token_hash,
                              display=self.owner_principal,
                              account_id="acct_owner")
        except Exception as exc:                        # noqa: BLE001 - 遷移失敗不擋開機
            print(f"  accounts: 主人帳號遷移失敗（{type(exc).__name__}: {exc}）", flush=True)
            return
        # 已核可的 guest：把他們的明文 token 換成帳號（GuestStore 仍是核可流程的權威）
        try:
            for request in self.guests.accounts():
                # guests.json 只存雜湊（2026-10-31）；舊檔載入時已換成雜湊
                digest = str(getattr(request, "token_sha256", "") or "")
                if not digest:
                    continue
                # 主人不是 guest：舊版「主人輪替自己」會把自己寫進 guests.json（2026-10-31 複檢）⇒ 跳過，
                # 不然他的新 token 會被收編成 guest 帳號
                if str(getattr(request, "principal_id", "") or "") == self.owner_principal:
                    continue
                store.import_hash(role="guest", principal=request.principal_id,
                                  token_hash=digest,
                                  display=request.principal_id,
                                  capabilities=tuple(getattr(request, "capabilities", ())
                                                     or ("chat",)))
        except Exception as exc:                        # noqa: BLE001
            print(f"  accounts: guest 遷移略過（{type(exc).__name__}: {exc}）", flush=True)

    def account_for(self, auth: AuthContext) -> dict | None:
        """這個 AuthContext 對應的帳號（`/api/me` 用）。"""
        store = self.accounts
        if store is None or auth is None:
            return None
        try:
            return store.get(auth.token_id) or store.by_principal(auth.principal)
        except Exception:                               # noqa: BLE001
            return None

    # ── owner token ─────────────────────────────────────────────────────────────
    @property
    def owner_token(self) -> str:
        """明文 token（**只在建立/輪替的那一次**有值；其餘情況回空字串）。

        為什麼還留著：主人第一次要抄走它。啟動日誌只印 fingerprint，不印這個。
        """
        return self._token

    @property
    def owner_token_fingerprint(self) -> str:
        """給日誌用的指紋：`ot_ab12…（43 字）`——看得出來是哪一把，抄不走。"""
        token = self._token or self._token_hash
        if not token:
            return "（未設定）"
        return f"{token[:7]}…（{len(token)} 字）"

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    def owner_context(self, *, credential: str = "") -> AuthContext:
        return AuthContext(self.owner_principal, Role.OWNER,
                           frozenset({"chat", "owner"}), token_id="owner",
                           credential=str(credential or ""))

    def _load_or_create(self) -> tuple[str, bool]:
        """回 `(明文或空, 是否需要把明文升級成 hash)`。"""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            legacy = str(raw.get("owner_token") or "").strip()
            if legacy:
                self._token_hash = self._hash(legacy)
                return legacy, True                 # 舊格式：升級
            stored = str(raw.get("owner_token_sha256") or "").strip()
            if stored:
                self._token_hash = stored
                return "", False                    # 已是雜湊：本次啟動沒有明文可用
        except (FileNotFoundError, ValueError, OSError):
            pass
        token = "ot_" + secrets.token_urlsafe(32)
        self._token_hash = self._hash(token)
        self.created_now = True
        self._save_hash(self._token_hash)
        return token, False

    def rotate(self) -> str:
        """輪替 owner token：舊的立刻失效，回新的明文（只回這一次）。

        ⚠ 帳號層也要同步換雜湊，否則舊 token 會從 `data/accounts.json` 那條路繼續有效
        （2026-10-22 實測：輪替後舊 token 仍通過認證）。
        """
        token = "ot_" + secrets.token_urlsafe(32)
        self._token = token
        self._token_hash = self._hash(token)
        self._save_hash(self._token_hash)
        if self.accounts is not None:
            try:
                self.accounts.import_hash(role="master", principal=self.owner_principal,
                                          token_hash=self._token_hash,
                                          display=self.owner_principal,
                                          account_id="acct_owner")
            except Exception as exc:                    # noqa: BLE001
                print(f"  accounts: 輪替同步失敗（{type(exc).__name__}: {exc}）", flush=True)
        self.audit("auth.rotate")
        return token

    def adopt_owner_hash(self, token_hash: str) -> None:
        """換機匯入：主人 token 改成匯入檔裡那一個（只有雜湊）。

        帳號層已經由 `AccountStore.merge_rows` 換好；這裡把 `data/auth.json` 也換掉，
        否則下次啟動 `_migrate_accounts` 會用舊雜湊把 acct_owner 蓋回去（2026-10-31 複檢）。
        """
        digest = str(token_hash or "").strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("bad owner token hash")
        self._token = ""
        self._token_hash = digest
        self._save_hash(digest)
        self.audit("auth.adopt_owner_hash")

    def _save_hash(self, token_hash: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"owner_token_sha256": token_hash, "created_at": time.time()},
                          ensure_ascii=False)
        # 暫存檔名每個 process 唯一（固定 `.tmp` 會被兩個 writer 互相截斷）
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, body.encode("utf-8"))
        finally:
            os.close(fd)
        adopt_owner(tmp)                 # 擁有者跟 `data/` 對齊（主機側 986 才讀得到）
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o660)   # 同上：容器 root 寫、主機側 986 要讀得到
        except OSError:
            pass

    # ── 解析 ────────────────────────────────────────────────────────────────────
    def authenticate(self, authorization: str, *, path: str = "", ip: str = "") -> AuthContext | None:
        raw = str(authorization or "").strip()
        scheme, _, token = raw.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            if raw:
                self.audit("auth.invalid", path=path, ip=ip, code="bad_scheme")
            return None
        # 帳號層優先：撤銷／輪替要立刻生效（主人的 token 也收編在同一張表裡）
        if self.accounts is not None:
            row = self.accounts.verify(token)
            if row is not None:
                kind = str(row.get("role") or "")
                role = {"master": Role.OWNER, "service": Role.SERVICE}.get(kind, Role.GUEST)
                if role is Role.SERVICE and str(path or "") not in SERVICE_PATHS:
                    self.audit("auth.service_path", path=path, ip=ip, ok=False, code="service_path")
                    return None
                caps = frozenset(row.get("capabilities") or
                                 (["chat", "owner"] if role is Role.OWNER else ["chat"]))
                return AuthContext(str(row.get("principal") or self.owner_principal),
                                   role, caps, token_id=str(row.get("account_id") or ""),
                                   credential=token, device=str(row.get("device_id") or ""),
                                   source=str(row.get("source") or ""),
                                   entity=str(row.get("entity") or ""))
        if self._token_hash and secrets.compare_digest(self._hash(token),
                                                       self._token_hash):
            # legacy hash 只是相容路徑；帳號層已撤銷主人（或只撤了 primary 那台）時不能繞過撤銷恢復 owner。
            master = self.accounts.get("acct_owner") if self.accounts is not None else None
            if not master or not (master.get("revoked_at") or master.get("token_revoked_at")):
                return self.owner_context(credential=token)
        if token:
            request = self.guests.by_token(token)
            # 主人不是 guest（舊版輪替會把主人寫進 guests.json）⇒ 那筆不能拿來當 guest 身分
            if request is not None and request.principal_id != self.owner_principal:
                return AuthContext(
                    request.principal_id, Role.GUEST,
                    frozenset(request.capabilities or ("chat",)),
                    token_id=request.id, credential=token)
        self.audit("auth.invalid", path=path, ip=ip, code="unknown_token")
        return None

    def resolve_principal(self, auth: AuthContext, claimed: str = "", *,
                          source: str = "", external_id: str = "") -> str:
        """Guest 永远是自己；owner 可以代某人；**只有服务帐号**能按源外部 ID 认人。

        2026-11-16（Discord 源级认证，owner「每个 bot 一个 token」）：以前任何 owner token
        带上 `X-Source`＋`X-External-Id` 就能以任一 Discord 用户的身分说话（没有任何凭据担保
        「这条真的来自 Discord」）。现在：
          · 服务帐号：`source` 必须等于它绑定的源，外部 ID 必须已绑定 ⇒ 那个 principal；否则回空（呼叫端 403）；
          · owner／guest：外部 ID 头一律忽略。
        """
        if auth.role is Role.SERVICE:
            src, ext = str(source or "").strip().lower(), str(external_id or "").strip()
            if not src or not ext or src != str(auth.source or "").lower():
                return ""
            return str(self.principals.lookup(src, ext) or "")
        if auth.role is Role.GUEST:
            return auth.principal
        claim = norm(claimed, fallback="") if str(claimed or "").strip() else ""
        if claim and self.principals.get(claim) is not None:
            return claim
        return auth.principal

    # ── 限流 ────────────────────────────────────────────────────────────────────
    def allow(self, key: str, *, limit: int | None = None,
              window_s: float | None = None) -> bool:
        bucket_key = str(key or "anonymous")
        cap = max(1, int(limit if limit is not None else self.chat_limit))
        span = max(1.0, float(window_s if window_s is not None else self.window_s))
        now = time.time()
        with self._lock:
            bucket = self._hits.setdefault(bucket_key, deque())
            cutoff = now - span
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= cap:
                return False
            bucket.append(now)
            return True

    def allow_chat(self, auth: AuthContext) -> bool:
        return self.allow(f"chat:{auth.principal}", limit=self.chat_limit)

    def allow_join(self, ip: str) -> bool:
        return self.allow(f"join:{ip or 'local'}", limit=self.join_limit)

    # ── 审计 ────────────────────────────────────────────────────────────────────
    def audit(self, event: str, *, auth: AuthContext | None = None,
              path: str = "", ip: str = "", ok: bool = True, code: str = "") -> None:
        row = {
            "at": round(time.time(), 3), "event": str(event),
            "principal": auth.principal if auth is not None else "",
            "role": auth.role.value if auth is not None else "",
            "path": str(path or ""), "ip": str(ip or ""),
            "ok": bool(ok), "code": str(code or ""),
        }
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with self._audit_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass


__all__ = ["AuthService"]
