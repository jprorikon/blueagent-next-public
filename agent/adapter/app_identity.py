"""App 邊界的身分翻譯：App 帳號 id → 腦端 principal（owner 2026-11-09「核心账号层在脑端」）。

中樞在 `brain/app/identity.py`（`IdentityMap`，`state` 接線時掛到 `adapter.identity`）；這裡只是
adapter 用的兩個小函式，沒接中樞（測試／舊接線）就原樣回。角色（`mira`…）兩邊同名，不會被翻。
"""

from __future__ import annotations


def principal_of(adapter, account_id: str) -> str:
    identity = getattr(adapter, "identity", None)
    ident = str(account_id or "")
    return identity.principal_of(ident) if identity is not None and ident else ident


def people(adapter, ids) -> list[str]:
    """群聊名冊（App 帳號 id）→ principal，去重保序。"""
    out: list[str] = []
    for item in ids or ():
        who = principal_of(adapter, str(item))
        if who and who not in out:
            out.append(who)
    return out


__all__ = ["people", "principal_of"]
