"""核心帳號在腦端，外部帳號都綁在它身上（owner 2026-11-09）。

> 「对 agent 本身，核心账号层在脑端，主人可以映射到 discord 账号，app 账号等等等。」
> 「app 的主人和脑端的主人映射；app 的客人和脑端的对应客人映射。」

腦端的一個 principal（`owner`、`guest-…`）是**唯一的那個人**；他在 App 的帳號 id（`acct_owner`）、
在 Discord 的 user id，都是 `PrincipalRegistry` 裡 `(source, external_id) → principal` 的一條綁定
（DESIGN §3.2 本來就這麼設計，只是 App 那條一直沒綁：11-07 App Server 拆出來之後，App 私聊的
principal 一直是帳號 id——主人在 App 裡不被當成主人、記憶與線跟 Discord 分家）。

開機（與 `reset()`）時 `bind_identities(app)` 從三個地方把綁定建起來：

  · 帳號表（`data/accounts.json`）：每個帳號的 `links`；沒寫 `app` 的，App 帳號 id ＝ 腦端 account_id
    （App Server 的帳號由這張表產生，`tools/provision_app_server.py`）；
  · 客人台帳（`guests.json`）：核可過的客人的 Discord id；
  · Discord 設定：`ALLOWED_USER_IDS` 裡的人＝主人。

出入口只用 `IdentityMap`：進來 `principal_of(外部 id)`、出去 `account_id_of(principal)`。
查不到就原樣回（不會像 `resolve()` 那樣退回預設的主人——陌生人不能變成主人）。
"""

from __future__ import annotations

import os

from agent.brain.domain.principal import Principal

APP = "app"
DISCORD = "discord"


class IdentityMap:
    """某一個源（預設 App）的進出翻譯；方法名與 `AccountStore.principal_of／account_id_of` 相同，可以直接替換。"""

    def __init__(self, principals, *, source: str = APP) -> None:
        self.principals = principals
        self.source = str(source)

    def principal_of(self, external_id: str) -> str:
        ident = str(external_id or "").strip()
        if not ident:
            return ""
        return self.principals.lookup(self.source, ident) or ident

    def account_id_of(self, principal: str, *, role: str = "") -> str:
        who = str(principal or "").strip()
        found = self.principals.externals(who, self.source) if who else []
        return found[0] if found else who

    def people(self, ids) -> list[str]:
        """一串外部 id（群聊名冊）→ principal，去重保序。"""
        out: list[str] = []
        for item in ids or ():
            who = self.principal_of(str(item))
            if who and who not in out:
                out.append(who)
        return out


def _ensure(principals, principal: str, *, display: str, owner: bool) -> bool:
    if not principal or principals.get(principal) is not None:
        return principals.get(principal) is not None
    if owner:
        return False                                   # 主人只有一個（`build_principals` 已登記）
    principals.register(Principal(principal, display or principal, role="guest"))
    return True


def bind_identities(app, *, env=None) -> dict:
    """把 App／Discord 的外部身分綁到腦端 principal。回 `{source: 綁了幾條}`（給日誌）。"""
    env = os.environ if env is None else env
    principals = getattr(app, "principals", None)
    if principals is None:
        return {}
    counts = {APP: 0, DISCORD: 0}

    def bind(source: str, external_id: str, principal: str) -> None:
        try:
            principals.bind(source, external_id, principal)
            counts[source] = counts.get(source, 0) + 1
        except (KeyError, ValueError):
            pass

    accounts = getattr(app, "accounts", None)
    for row in (accounts.list() if accounts is not None else []):
        if str(row.get("role") or "") == "service":
            continue                                   # bot 不是人：不登記成 principal、不綁外部身分
        principal = str(row.get("principal") or "").strip()
        owner = str(row.get("role") or "") == "master"
        if owner:
            principal = principals.default()
        if not _ensure(principals, principal, display=str(row.get("display") or ""), owner=owner):
            continue
        for source, ids in accounts.links(str(row.get("account_id") or "")).items():
            for external_id in ids:
                bind(source, external_id, principal)
    guests = getattr(app, "guest_store", None)
    for request in (guests.accounts() if guests is not None and hasattr(guests, "accounts") else []):
        if getattr(request, "discord_id", "") and principals.get(request.principal_id) is not None:
            bind(DISCORD, request.discord_id, request.principal_id)
    for external_id in str(env.get("ALLOWED_USER_IDS") or "").split(","):
        if external_id.strip():
            bind(DISCORD, external_id.strip(), principals.default())
    app.identity = IdentityMap(principals)
    return counts


__all__ = ["APP", "DISCORD", "IdentityMap", "bind_identities"]
