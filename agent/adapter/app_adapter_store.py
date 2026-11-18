"""App adapter 的旁挂档：事件失败计数／dead-letter／未提交的回复。

为什么单独一个档：游标档只存「读到哪」，但 REVIEW 2026-11-07 的两条高危
（#2 提交失败丢回复、#3 一件处理不了的事件卡死整条线）都需要**游标旁边的簿记**：

  · `<实体>-unsent.json`    —— 回合跑完、提交还没成功的那份回复（重试只补发，不重跑）；
  · `<实体>-dead-letter.json` —— 每个事件的连续失败次数 ＋ 已放弃的取证记录。

放在 `AppChatAdapter` 里当然也行，但它会撑爆单档 600 行的棘轮，
而这两件事跟「订阅循环」的职责本来就不同（一个是簿记，一个是调度）。
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import time


#: 这些错误重试一辈子也不会自己好（REVIEW 2026-11-07 #3）：图片被删（404）、
#: 没有权限（403）、内容不合法／过大（media_invalid／sticker_invalid）⇒ 直接 dead-letter。
#: 网络抖动、502、超时一律不在里面：那些要给足重试次数。
#: `_request` 遇到带 JSON 的错误体时码是错误名（`app_server_http_not_found`），
#: 只有 `read_media` 用数字码——两种写法都要认（REVIEW 2026-11-08）。
FATAL_PREFIXES = ("app_server_http_404", "app_server_http_403", "app_server_http_not_found",
                  "app_server_http_forbidden", "app_server_media_invalid",
                  "app_server_sticker_invalid")


def fatal_failure(exc: BaseException) -> bool:
    text = str(exc or "")
    return any(text.startswith(prefix) for prefix in FATAL_PREFIXES)


class AdapterSidecar:
    """游标档旁边的簿记。所有写都是「临时档 → rename」，坏档一律当空的。"""

    #: dead-letter 只留最近这么多条取证（不是状态，纯粹给人看）
    MAX_DEAD_ROWS = 200

    def __init__(self, cursor_path) -> None:
        self.cursor_path = pathlib.Path(cursor_path)
        #: 拉取執行緒與回合工作執行緒共用（2026-11-16 群聊回合移出拉取執行緒）：讀改寫要成套
        self.lock = threading.RLock()
        self.failures = self._load_failures()
        self.unsent = self._load_unsent()

    # ── 档位 ───────────────────────────────────────────────────────────────────
    def path_of(self, suffix: str) -> pathlib.Path:
        name = self.cursor_path.name
        if name.endswith(".json"):
            name = name[: -len(".json")]
        if name.endswith("-cursor"):
            name = name[: -len("-cursor")]
        return self.cursor_path.with_name(f"{name}-{suffix}.json")

    def read(self, suffix: str) -> dict:
        try:
            payload = json.loads(self.path_of(suffix).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def write(self, suffix: str, payload: dict) -> None:
        path = self.path_of(suffix)
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 臨時檔名帶執行緒 id：兩條執行緒同時寫同一個檔也不會搶同一個 .tmp
            temporary = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True),
                                 encoding="utf-8")
            os.replace(temporary, path)

    # ── 失败计数／dead-letter ───────────────────────────────────────────────────
    def _load_failures(self) -> dict:
        rows = self.read("dead-letter").get("failures") or {}
        return {str(key): int(value) for key, value in rows.items()
                if str(key) and str(value).isdigit()}

    def note_failure(self, key: str) -> int:
        with self.lock:
            count = int(self.failures.get(key, 0)) + 1
            self.failures[key] = count
            payload = self.read("dead-letter")
            payload["failures"] = dict(self.failures)
            self.write("dead-letter", payload)
            return count

    def clear_failure(self, key: str) -> None:
        with self.lock:
            if self.failures.pop(key, None) is None:
                return
            payload = self.read("dead-letter")
            payload["failures"] = dict(self.failures)
            self.write("dead-letter", payload)

    def dead_letter(self, key: str, exc: BaseException, event: dict, *,
                    entity: str = "", log=None) -> None:
        with self.lock:
            payload = self.read("dead-letter")
            rows = [row for row in (payload.get("dead") or []) if isinstance(row, dict)]
            rows.append({"event_id": str(event.get("event_id") or key),
                         "cursor": str(event.get("cursor") or ""),
                         "type": type(exc).__name__, "reason": str(exc)[:300],
                         "at": time.time()})
            payload["dead"] = rows[-self.MAX_DEAD_ROWS:]
            self.failures.pop(key, None)
            payload["failures"] = dict(self.failures)
            self.write("dead-letter", payload)
        if log is not None:
            log(f"app adapter[{entity}]: dead-letter event={key} "
                f"{type(exc).__name__}: {str(exc)[:160]} ⇒ 跳过（游标照推进）")

    # ── 未提交的回复 ───────────────────────────────────────────────────────────
    def _load_unsent(self) -> dict:
        rows = self.read("unsent").get("replies") or {}
        return {str(key): value for key, value in rows.items() if isinstance(value, dict)}

    def put_unsent(self, key: str, entry: dict) -> None:
        with self.lock:
            self.unsent[str(key)] = dict(entry)
            self.write("unsent", {"replies": dict(self.unsent)})

    def drop_unsent(self, key: str) -> None:
        with self.lock:
            if self.unsent.pop(str(key), None) is None:
                return
            self.write("unsent", {"replies": dict(self.unsent)})


__all__ = ["AdapterSidecar", "FATAL_PREFIXES", "fatal_failure"]
