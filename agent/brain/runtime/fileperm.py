"""檔案擁有者對齊：容器以 root 跑，但 `data/` 是主機側（986）要讀寫的共用目錄。

為什麼要這支（2026-10-31）：容器 root 寫出來的檔一律 root:root，
主機側的 `tools/`（rotate_owner_token、doctor、backfill）以 986 跑就讀不到——
`accounts.json` 還因此被「讀不到就隔離」誤判成壞檔（一天 12 次）。

這台 NAS 的檔案系統不吃 setgid 繼承（`chmod g+s` 之後新檔的 group 仍是 root），
所以改成**寫完就地 chown 成「父資料夾的擁有者」**：資料夾是誰的，新檔就是誰的。
chown 失敗（非 root、或檔案系統不給）就安靜跳過——那是加分項，不該讓存檔失敗。

⚠️ **不是**「容器寫的每個檔」都會經過這裡：目前只接在主機側工具會碰的幾處——
`accounts.json`、`auth.json`（0660：owner token 的**雜湊**對 gid 980 可讀，這是刻意的，
rotate_owner_token／doctor 要讀）、`life-rotation.json`、`notes/`、`works/`。
其他檔（記憶庫、媒體、活動存檔…）仍是 root 擁有，只靠 `run.py` 的 umask 002 讓 group 讀得到；
新增一個主機側要寫的檔，記得在寫檔處接上 `adopt_owner`。
"""

from __future__ import annotations

import os
import pathlib


def adopt_owner(path, *, like=None) -> None:
    """把 `path` 的擁有者改成 `like`（預設＝它的父資料夾）的擁有者。"""
    try:
        source = pathlib.Path(like) if like is not None else pathlib.Path(path).parent
        info = source.stat()
        os.chown(path, info.st_uid, info.st_gid)
    except OSError:
        pass


__all__ = ["adopt_owner"]
