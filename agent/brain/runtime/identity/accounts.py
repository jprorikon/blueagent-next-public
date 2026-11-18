"""帳號層：**主人（master）與客人（guest）用同一套帳號物件**，token 只存雜湊。

為什麼要這一層（owner 2026-10-22：「不需要為主人和 guest 準備帳號層麼？」）：
  Unity 要拆 master 端／guest 端，端末第一件事就是問「我是誰、我能做什麼」。
  在那之前，身分是散在三個地方的：
    · 主人 ⇒ `data/auth.json` 一個 hash（沒有帳號 id、不能列、不能撤）；
    · 客人 ⇒ `GuestStore`（申請／核可流程的產物，token 還是明文）；
    · 端末 ⇒ 只能從「哪些 API 回 403」反推自己是誰。

這一層做的事：
  · 一個帳號一筆：`account_id`／role（`master` / `guest`）／principal／顯示名／能力／token 雜湊；
  · 簽發、輪替、撤銷、列表；`verify(token)` 回帳號（順手更新 `last_seen_at`）；
  · **只存 SHA-256**，檔案 0600、原子寫入；
  · 舊資料（主人 token、已核可的 guest token）在啟動時**自動匯入**，不把人鎖在外面。

不動既有語義：`AuthContext.role` 仍然是 `owner` / `guest`；這裡的 `master` 是給客戶端看的稱呼，
對應 `Role.OWNER`（一個 bot／一個 runtime 只有一個 master）。
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import secrets
import threading
import time

from agent.brain.runtime.fileperm import adopt_owner
from agent.brain.runtime.identity import devices as device_rows

ROLE_MASTER = "master"
ROLE_GUEST = "guest"
#: 服務帳號（owner 2026-11-16「每個 bot 一個 token」）：只代表某個源（`discord`）的某一顆 bot，
#: 只能替那個源上**已綁定**的外部身分發回合，不是人、不能進管理面
ROLE_SERVICE = "service"
ROLES = (ROLE_MASTER, ROLE_GUEST, ROLE_SERVICE)

#: 主人預設拿到的能力（管理面全部；`chat` 一定要有）
MASTER_CAPABILITIES = ("chat", "admin", "manage_accounts", "manage_guests", "delegate")
#: 客人預設能力
GUEST_CAPABILITIES = ("chat",)
#: 服務帳號的能力（源與實體另外存在 `source`／`entity` 欄）
SERVICE_CAPABILITIES = ("chat",)

PREFIX = "ba"


def hash_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _new_token() -> str:
    # `ba_<32 hex>`：看得出是我們的 token，長度夠、可貼可掃
    return f"{PREFIX}_{secrets.token_hex(24)}"


class AccountStore:
    """帳號的持久化（`data/accounts.json`，0660：容器 root 與主機側 gid 980 共用）。"""

    #: 唯讀模式下最多每幾秒重讀一次原檔（驗證失敗的請求很多，別每次都碰磁碟）
    RECOVER_EVERY_S = 30.0

    def __init__(self, path, *, log=None, clock=time.time) -> None:
        self.path = pathlib.Path(path)
        self._log = log or (lambda _message: None)
        self._clock = clock
        self._lock = threading.RLock()
        self._rows: dict[str, dict] = {}
        #: 讀不到檔（權限／IO）⇒ 唯讀模式：**讀回來之前不覆蓋原檔**（寧可這次不存，也不要吃掉帳號）
        self._readonly = ""
        #: 上一次嘗試解除唯讀的時刻（`_recover_locked` 節流用；第一次立刻試）
        self._recover_at = float("-inf")
        self._load()

    # ── 讀寫檔 ──────────────────────────────────────────────────────────────
    def _load(self) -> None:
        """讀 `accounts.json`。

        ⚠️ 2026-10-31：**讀不出來時不能靜靜地當成「空庫」**。空庫會讓啟動時的 legacy 遷移
        （`AuthService._migrate_legacy`）立刻把 owner ＋ `guests.json` 那批舊 guest 重新寫進來，
        而 **App 建的那些 guest 只存在這個檔裡** ⇒ 一次讀取失敗就永久消失（回報症狀：重建容器後
        只剩 owner 與 guest-707）。所以：壞檔改名留證（`.corrupt-<時間>`）並大聲記錄，絕不覆蓋證據。

        ⚠️ 2026-10-31 第二輪（同一天 12 次誤隔離）：**「讀不到」不等於「檔壞了」**。
        容器以 root 跑（檔案 root:root 0600），而主機側工具以 986 跑 ⇒ 一次 `PermissionError`
        就把好檔改名隔離、接著以空庫啟動（再靠 `guests.json` 播回來）。現在分兩種處理：
          · `ValueError`（JSON 真的壞）⇒ 改名留證（證據優先）；
          · `OSError`（權限／暫時讀不到）⇒ 重試幾次，仍失敗就**唯讀模式**（絕不覆蓋原檔）。
        """
        if not self.path.exists():
            return                                   # 第一次啟動：沒有檔是正常的
        raw, last_error = "", None
        for attempt in range(3):
            try:
                raw = self.path.read_text(encoding="utf-8")
                last_error = None
                break
            except OSError as exc:                   # 權限／暫時讀不到：重試，別急著判死
                last_error = exc
                time.sleep(0.05 * (attempt + 1))
        if last_error is not None:
            self._readonly = f"{type(last_error).__name__}: {last_error}"
            self._log(f"accounts: 讀不到 {self.path.name}（{self._readonly}）"
                      "⇒ 這次**唯讀啟動**（不覆蓋原檔；請確認檔案權限）")
            return
        try:
            data = json.loads(raw)
        except ValueError as exc:
            self._quarantine(exc)
            return
        rows = data.get("accounts") if isinstance(data, dict) else None
        if not isinstance(rows, dict):
            self._quarantine(ValueError("格式不對：沒有 accounts 物件"))
            return
        self._rows = {str(key): dict(value) for key, value in rows.items()
                      if isinstance(value, dict)}

    def _quarantine(self, exc: BaseException) -> None:
        """把讀不了的檔改名留證（不刪），並喊出來——寧可吵，也不要默默把帳號弄丟。"""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        keep = self.path.with_name(self.path.name + ".corrupt-" + stamp)
        try:
            self.path.replace(keep)
            self._log(f"accounts: 讀不出 {self.path.name}（{type(exc).__name__}: {exc}）"
                      f"⇒ 原檔保留為 {keep.name}，這次以空庫啟動")
        except OSError:
            self._log(f"accounts: 讀不出 {self.path.name}（{type(exc).__name__}: {exc}）"
                      f"且改名失敗 ⇒ 這次以空庫啟動（原檔仍在，快備份）")

    def _recover_locked(self) -> bool:
        """唯讀模式下再讀一次原檔；讀得到就把原檔的帳號併回來、解除唯讀（不用重啟）。

        以前一進唯讀就要撐到下次重啟：這段期間只有 owner 與 `guests.json` 那批在手上，
        App 建的 guest 全部 401。現在每 `RECOVER_EVERY_S` 秒最多重讀一次（存檔／驗證不到時觸發）：
          · 原檔的列優先（那才是真的名單）；
          · 唯讀期間手上多出來的列（遷移播回來的、這段時間新簽的）只補「原檔沒有的 id、也沒有同一個雜湊」的；
          · 原檔 JSON 真的壞了 ⇒ 維持唯讀（不覆蓋、也不在這裡改名，留給人看）。
        """
        if not self._readonly:
            return True
        now = float(self._clock())
        if now - self._recover_at < self.RECOVER_EVERY_S:
            return False
        self._recover_at = now
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        rows = data.get("accounts") if isinstance(data, dict) else None
        if not isinstance(rows, dict):
            return False
        merged = {str(key): dict(value) for key, value in rows.items() if isinstance(value, dict)}
        by_hash = {str(row.get("token_sha256") or ""): ident for ident, row in merged.items()}
        added = 0
        for ident, row in self._rows.items():
            digest = str(row.get("token_sha256") or "")
            target = ident if ident in merged else by_hash.get(digest)
            if target is None:
                merged[ident] = row
                added += 1
                continue
            # 同一個帳號：唯讀期間做過的事（輪替／撤銷／最近登入）不能因為併回來就消失
            kept = merged[target]
            if target == ident:
                kept["token_sha256"] = digest or kept.get("token_sha256", "")
            for field in ("revoked_at", "last_seen_at"):
                kept[field] = max(float(kept.get(field) or 0.0), float(row.get(field) or 0.0))
        self._rows = merged
        reason, self._readonly = self._readonly, ""
        self._log(f"accounts: {self.path.name} 讀得到了（之前：{reason}）⇒ 解除唯讀，"
                  f"原檔 {len(rows)} 筆＋唯讀期間新增 {added} 筆")
        return True

    @property
    def readonly(self) -> bool:
        """現在是不是唯讀（`/api/health` 用；只給布林，不外洩路徑與錯誤訊息）。"""
        return bool(self._readonly)

    def _save_locked(self) -> None:
        if self._readonly and not self._recover_locked():
            self._log(f"accounts: 唯讀模式（{self._readonly}）⇒ 這次不寫檔，原檔保持不動")
            return
        body = json.dumps({"version": 1, "accounts": self._rows},
                          ensure_ascii=False, indent=1) + "\n"
        # ⚠️ 暫存檔名要**每個 process 唯一**：以前固定叫 `accounts.json.tmp`，
        #    兩個 writer（容器重啟時新舊重疊、或主機側工具同時寫）會互相截斷 ⇒ 產出壞檔。
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(body, encoding="utf-8")
            os.chmod(tmp, 0o660)     # group(980)=trim.hermes 也要能讀寫（容器 root 與主機側共用 data/）
            adopt_owner(tmp)        # 擁有者跟 `data/` 對齊（這台不吃 setgid 繼承）
            tmp.replace(self.path)
        except OSError as exc:
            self._log(f"accounts: 存檔失敗（{type(exc).__name__}: {exc}）")
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    # ── 簽發／匯入 ──────────────────────────────────────────────────────────
    def issue(self, *, role: str, principal: str, display: str = "",
              capabilities=None, account_id: str = "", token: str = "",
              source: str = "", entity: str = "") -> tuple[dict, str]:
        """簽一個新帳號，回 `(帳號, 明文 token)`；明文只出現這一次。

        `role="service"` 要帶 `source`（例：`discord`）與 `entity`（哪一顆 bot 的角色）。
        """
        role = str(role or ROLE_GUEST).strip().lower()
        if role not in ROLES:
            raise ValueError(f"unknown role: {role}")
        source, entity = str(source or "").strip().lower(), str(entity or "").strip().lower()
        if role == ROLE_SERVICE and (not source or not entity):
            raise ValueError("service account needs source and entity")
        plain = str(token or "").strip() or _new_token()
        caps = tuple(capabilities or (MASTER_CAPABILITIES if role == ROLE_MASTER
                                      else SERVICE_CAPABILITIES if role == ROLE_SERVICE
                                      else GUEST_CAPABILITIES))
        with self._lock:
            ident = str(account_id or "").strip() or self._next_id_locked(role)
            row = {
                "account_id": ident, "role": role,
                "principal": str(principal or ident),
                "display": str(display or principal or ident),
                "capabilities": list(caps),
                "token_sha256": hash_token(plain),
                "created_at": round(self._clock(), 3), "last_seen_at": 0.0,
                "revoked_at": 0.0, "note": "",
            }
            if role == ROLE_SERVICE:
                row["source"], row["entity"] = source, entity
            self._rows[ident] = row
            self._save_locked()
        return dict(row), plain

    def import_hash(self, *, role: str, principal: str, token_hash: str,
                    display: str = "", capabilities=None,
                    account_id: str = "") -> dict:
        """把既有的 token 雜湊收編進來（啟動時的一次性遷移，不動原本的驗證路徑）。"""
        role = str(role or ROLE_GUEST).strip().lower()
        digest = str(token_hash or "").strip()
        if not digest:
            raise ValueError("token_hash required")
        with self._lock:
            for row in self._rows.values():
                if row.get("token_sha256") == digest:
                    return dict(row)
            ident = str(account_id or "").strip() or self._next_id_locked(role)
            existing = self._rows.get(ident)
            if existing is not None:
                # 同一個帳號（例如主人輪替 token）⇒ 更新雜湊，不要長出第二個
                existing["token_sha256"] = digest
                existing["role"] = role
                existing["principal"] = str(principal or existing.get("principal") or ident)
                existing["capabilities"] = list(capabilities or existing.get("capabilities")
                                                or GUEST_CAPABILITIES)
                self._save_locked()
                return dict(existing)
            row = {"account_id": ident, "role": role,
                   "principal": str(principal or ident),
                   "display": str(display or principal or ident),
                   "capabilities": list(capabilities or (MASTER_CAPABILITIES
                                                         if role == ROLE_MASTER
                                                         else GUEST_CAPABILITIES)),
                   "token_sha256": digest,
                   "created_at": round(self._clock(), 3), "last_seen_at": 0.0,
                   "revoked_at": 0.0, "note": "imported"}
            self._rows[ident] = row
            self._save_locked()
            return dict(row)

    def _next_id_locked(self, role: str) -> str:
        stem = {ROLE_MASTER: "owner", ROLE_SERVICE: "svc"}.get(role, "g")
        for _ in range(50):
            ident = f"acct_{stem}_{secrets.token_hex(3)}"
            if ident not in self._rows:
                return ident
        return f"acct_{stem}_{int(self._clock())}"

    # ── 驗證／輪替／撤銷 ────────────────────────────────────────────────────
    def verify(self, token: str, *, touch: bool = True) -> dict | None:
        """token ⇒ 帳號（副本，多一個 `device_id`：是這個帳號的哪一台端末）。"""
        digest = hash_token(token)
        if not digest:
            return None
        with self._lock:
            row, device = self._match_locked(digest)
            # 唯讀中（開機讀不到原檔）⇒ 手上的名單不完整：找不到就試著把原檔讀回來再找一次
            if row is None and self._readonly and self._recover_locked():
                row, device = self._match_locked(digest)
            if row is None:
                return None
            if touch:
                device_rows.touch(row, device, self._clock())
                self._save_locked()
            found = dict(row)
            found["device_id"] = device
            return found

    def verify_hash(self, digest: str) -> dict | None:
        """只有雜湊時的同一條驗證（App Server 問「這把是誰」用；不更新最後使用時間）。"""
        digest = str(digest or "").strip().lower()
        if len(digest) != 64:
            return None
        with self._lock:
            row, device = self._match_locked(digest)
            if row is None:
                return None
            found = dict(row)
            found["device_id"] = device
            return found

    def _match_locked(self, digest: str) -> tuple[dict | None, str]:
        for row in self._rows.values():
            if row.get("revoked_at"):
                continue
            device = device_rows.match(row, digest)
            if device:
                return row, device
        return None, ""

    # ── 多台端末（owner 2026-11-16）─────────────────────────────────────────
    def add_device(self, account_id: str, name: str) -> tuple[dict, str]:
        """幫這個帳號簽一台新端末，回 `(端末, 明文 token)`；明文只出現這一次。"""
        plain = _new_token()
        with self._lock:
            row = self._rows.get(str(account_id))
            if row is None or row.get("revoked_at"):
                raise KeyError(f"unknown account: {account_id}")
            if str(row.get("role") or "") == ROLE_SERVICE:
                raise ValueError("service accounts have no devices")
            device = device_rows.add(row, name=name, token_hash=hash_token(plain),
                                     now=self._clock())
            self._save_locked()
        return device, plain

    def devices(self, account_id: str, *, current: str = "") -> list[dict]:
        row = self.get(account_id) or {}
        return device_rows.public(row, current=current) if row else []

    def revoke_device(self, account_id: str, device_id: str) -> bool:
        with self._lock:
            row = self._rows.get(str(account_id))
            if row is None or not device_rows.revoke(row, str(device_id), self._clock()):
                return False
            self._save_locked()
            return True

    def rotate(self, account_id: str) -> tuple[dict, str] | None:
        with self._lock:
            row = self._rows.get(str(account_id))
            if row is None:
                return None
            plain = _new_token()
            row["token_sha256"] = hash_token(plain)
            row["last_seen_at"] = 0.0
            row["token_revoked_at"] = 0.0          # 輪替出來的新 primary 是有效的
            self._save_locked()
            return dict(row), plain

    def revoke(self, account_id: str) -> bool:
        with self._lock:
            row = self._rows.get(str(account_id))
            if row is None:
                return False
            row["revoked_at"] = round(self._clock(), 3)
            self._save_locked()
            return True

    # ── 查詢 ────────────────────────────────────────────────────────────────
    def get(self, account_id: str) -> dict | None:
        with self._lock:
            row = self._rows.get(str(account_id))
            return dict(row) if row else None

    def list(self, *, include_revoked: bool = False) -> list[dict]:
        with self._lock:
            rows = [dict(row) for row in self._rows.values()
                    if include_revoked or not row.get("revoked_at")]
        rows.sort(key=lambda row: (row.get("role") != ROLE_MASTER,
                                   float(row.get("created_at") or 0)))
        return rows

    def by_principal(self, principal: str, *, role: str = "") -> dict | None:
        want = str(principal or "").strip()
        with self._lock:
            for row in self._rows.values():
                if row.get("revoked_at"):
                    continue
                if str(row.get("principal")) != want:
                    continue
                if role and str(row.get("role")) != str(role):
                    continue
                return dict(row)
        return None

    # ── 外部身分（owner 2026-11-09：核心帳號在腦端，App／Discord 帳號都綁在它身上）──────────
    def link(self, account_id: str, source: str, external_id: str) -> dict:
        """把一個外部帳號（`app`／`discord`…）綁到這個腦端帳號。同源可以綁多個（例：兩個 Discord 帳號）。"""
        src, ext = str(source or "").strip().lower(), str(external_id or "").strip()
        if not src or not ext:
            raise ValueError("link() 需要 source / external_id")
        with self._lock:
            row = self._rows.get(str(account_id))
            if row is None:
                raise KeyError(f"unknown account: {account_id}")
            links = row.setdefault("links", {})
            items = [str(item) for item in (links.get(src) or [])]
            if ext not in items:
                links[src] = items + [ext]
                self._save_locked()
            return dict(row)

    def unlink(self, account_id: str, source: str, external_id: str) -> dict:
        """解除一個外部帳號的綁定（只動 `links`；預設的 App 綁定＝account_id 本身，解不掉）。"""
        src, ext = str(source or "").strip().lower(), str(external_id or "").strip()
        with self._lock:
            row = self._rows.get(str(account_id))
            if row is None:
                raise KeyError(f"unknown account: {account_id}")
            links = row.setdefault("links", {})
            items = [str(item) for item in (links.get(src) or []) if str(item) != ext]
            if items:
                links[src] = items
            else:
                links.pop(src, None)
            self._save_locked()
            return dict(row)

    def links(self, account_id: str) -> dict[str, list[str]]:
        """這個帳號綁了哪些外部身分（沒寫過 `app` 的，App 帳號 id 就是腦端 account_id：
        App Server 的帳號是從這張表產生的，`tools/provision_app_server.py`）。"""
        row = self.get(account_id) or {}
        links = {str(src): [str(item) for item in (items or [])]
                 for src, items in (row.get("links") or {}).items()}
        links.setdefault("app", [str(row.get("account_id") or account_id)])
        return links

    # ── principal ↔ 平台帳號 id（2026-11-09）──────────────────────────────────
    # 平台（App Server）用帳號 id（`acct_owner`），大腦用 principal（`owner`）。
    # 同一件事在兩邊叫不同名字，跨過邊界要翻譯，不然「她送出的那一則在哪、他讀到哪」對不上。
    def principal_of(self, identity: str) -> str:
        """帳號 id 或 principal ⇒ 大腦的 principal（查不到就原樣回）。"""
        ident = str(identity or "").strip()
        if not ident:
            return ""
        row = self.get(ident)
        if row is not None and str(row.get("principal") or "").strip():
            return str(row["principal"])
        if self.by_principal(ident) is not None:
            return ident
        return ident

    def account_id_of(self, principal: str, *, role: str = "") -> str:
        """大腦的 principal ⇒ 平台帳號 id（沒有對應帳號就原樣回，讓呼叫端自己失敗）。"""
        want = str(principal or "").strip()
        if not want:
            return ""
        row = self.by_principal(want, role=role)
        if row is not None and str(row.get("account_id") or "").strip():
            return str(row["account_id"])
        return want

    def snapshot(self) -> dict:
        rows = self.list(include_revoked=True)
        return {"total": len(rows),
                "master": sum(1 for row in rows if row.get("role") == ROLE_MASTER
                              and not row.get("revoked_at")),
                "guest": sum(1 for row in rows if row.get("role") == ROLE_GUEST
                             and not row.get("revoked_at")),
                "revoked": sum(1 for row in rows if row.get("revoked_at"))}

    # ── 匯出／匯入（換機、遷移；owner 2026-10-31 第 3 項）────────────────────
    def export_rows(self) -> list[dict]:
        """完整匯出（含 token **雜湊**，不含明文）。換機時靠它保住「已經貼在端末上的 token」。"""
        with self._lock:
            return [dict(row) for row in self._rows.values()]

    def merge_rows(self, rows) -> dict:
        """匯入：以 `account_id` 為鍵 upsert，**不刪任何現有帳號**。

        壞列（缺 id／role 不認／雜湊格式不對）跳過並回報原因；同 id 但 role 不同 ⇒ 跳過
        （避免一份舊匯出把主人降級成 guest）。
        """
        if not isinstance(rows, list):
            raise ValueError("rows must be a list")
        imported = updated = 0
        skipped: list[dict] = []
        #: 主人帳號的雜湊被換掉時，回新雜湊 ⇒ 呼叫端要同步 `data/auth.json`（不然重啟會被蓋回去）
        master_hash = ""
        with self._lock:
            for raw in rows:
                if not isinstance(raw, dict):
                    skipped.append({"account_id": "", "reason": "not_an_object"})
                    continue
                ident = str(raw.get("account_id") or "").strip()
                role = str(raw.get("role") or "").strip().lower()
                digest = str(raw.get("token_sha256") or "").strip().lower()
                if not ident:
                    skipped.append({"account_id": "", "reason": "missing_account_id"})
                    continue
                if role not in ROLES:
                    skipped.append({"account_id": ident, "reason": "unknown_role"})
                    continue
                if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                    skipped.append({"account_id": ident, "reason": "bad_token_hash"})
                    continue
                existing = self._rows.get(ident)
                if existing is None:
                    self._rows[ident] = {
                        "account_id": ident, "role": role,
                        "principal": str(raw.get("principal") or ident),
                        "display": str(raw.get("display") or raw.get("principal") or ident),
                        "capabilities": list(raw.get("capabilities") or
                                             (MASTER_CAPABILITIES if role == ROLE_MASTER
                                              else GUEST_CAPABILITIES)),
                        "token_sha256": digest,
                        "created_at": float(raw.get("created_at") or self._clock()),
                        "last_seen_at": float(raw.get("last_seen_at") or 0.0),
                        "revoked_at": float(raw.get("revoked_at") or 0.0),
                        "note": str(raw.get("note") or "imported"),
                    }
                    for extra in ("devices", "token_revoked_at", "source", "entity", "links"):
                        if raw.get(extra):
                            self._rows[ident][extra] = raw[extra]
                    imported += 1
                    continue
                if str(existing.get("role") or "") != role:
                    skipped.append({"account_id": ident, "reason": "role_conflict"})
                    continue
                if role == ROLE_MASTER and existing.get("token_sha256") != digest:
                    master_hash = digest
                existing["token_sha256"] = digest               # 端末上的 token 得以沿用
                existing["capabilities"] = list(raw.get("capabilities") or
                                                existing.get("capabilities") or GUEST_CAPABILITIES)
                existing["display"] = str(raw.get("display") or existing.get("display") or "")
                if raw.get("revoked_at"):
                    existing["revoked_at"] = float(raw.get("revoked_at") or 0.0)
                updated += 1
            if imported or updated:
                self._save_locked()
        return {"imported": imported, "updated": updated, "skipped": skipped,
                "master_hash": master_hash}


def public_row(row: dict) -> dict:
    """給端末看的帳號摘要：**永遠不含 token／雜湊**。"""
    return {"account_id": row.get("account_id", ""),
            "role": row.get("role", ROLE_GUEST),
            "principal": row.get("principal", ""),
            "display": row.get("display", ""),
            "capabilities": list(row.get("capabilities") or ()),
            "created_at": row.get("created_at", 0.0),
            "last_seen_at": row.get("last_seen_at", 0.0),
            "revoked": bool(row.get("revoked_at")),
            "note": row.get("note", ""),
            **({"source": row.get("source", ""), "entity": row.get("entity", "")}
               if row.get("role") == ROLE_SERVICE else {})}


__all__ = ["AccountStore", "GUEST_CAPABILITIES", "MASTER_CAPABILITIES", "ROLES",
           "ROLE_GUEST", "ROLE_MASTER", "ROLE_SERVICE", "SERVICE_CAPABILITIES", "hash_token",
           "public_row"]
