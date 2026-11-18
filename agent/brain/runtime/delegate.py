"""委任本體（work 模式真的去幹活）—— owner 2026-10-21 的 M4。

四條紀律，全部照現役橋（它們都是踩出來的，不是設計出來的）：

1. **只有主人能委任**：`mode=work` 已經在 HTTP 闸门降级 guest（`work_requires_owner`），
   这里再加一道：工具面本身对 guest 不出现，`run()` 也直接拒绝。
2. **`{{OUT}}` 占位符**：产出文件名由代码生成，模型只写 `{{OUT}}`（自己起名会跟约定名撞车，
   现役实测撞过：cp 同名失败；写太严又会让模型放弃委任）。扩展名由模型自己加。
3. **去程附注 / 回程剥路径**：她看不到内部路径就不会把路径写进回话（现役真机事故：
   她照着回报写了「保存先はここ：/vol1/…png」）。交给模型的那份剥掉路径；
   给前端的 `artifact` 帧仍带路径（那是主人看的，不是模型看的）。
4. **交付回执**：模型看不到落点就无法确认「已交付」，会自己发明核查任务、同一件事委任到上限
   （现役那天：6 次委任、8 分钟、零回覆）。这里用「文件有没有出现」这个**状态事实**回执，
   回执里不含路径。

外加两条自己的约束：
* 同一轮同一句话不重复委任（按 task 归一化去重）；
* 一次委任有硬上限（默认 900 s），超时就回报「超时」而不是挂着。
"""

from __future__ import annotations

import pathlib
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Protocol


@dataclass
class DelegateOutcome:
    run_id: str = ""
    status: str = ""
    text: str = ""
    events: list[dict] = field(default_factory=list)
    previews: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.status in {"completed", "done", "ok"}


class DelegateGateway(Protocol):
    """委任通道（實作在 adapter 層；runtime 不 import adapters，也不寫死委任先是哪個 agent）。"""

    base_url: str

    def run(self, task: str, **kwargs) -> DelegateOutcome:
        ...

#: 去程附注：不要写出内部路径、交付名怎么用。
TAIL = (
    "\n\n【委任の約束】① 返事にファイルシステムのパスを書かない（「/… に保存しました」とは言わない）。やったことと結果だけを言う。"
    "② 成果物は上で決めたファイル名と拡張子のまま置く。自分で名前を変えない。"
    "③ 終わったら一、二文で結果を伝える。過程を言い直す必要はない。"
)
#: 回程剥除：交给模型的那份只留「有/没有」，不留位置。
_PATH = re.compile(r"(?:/(?:vol\d+|home|tmp|Users|root|mnt|data)[^\s，。！？,;:）)]*)")

#: 交付回执的措辞（状态事实，不含路径）。
#: 工具描述（`tools/delegate.py`）直接引用這三個常量——以前描述裡另寫一份「【交付回执】／已交付」，
#: 跟真的回執對不上，模型認不出「已經做完」就會再委任一次（review 2026-10-28）。
RECEIPT_TAG = "【交付の控え】"
RECEIPT_DONE_WORD = "届いた"
RECEIPT_DELIVERED = f"{RECEIPT_TAG}{RECEIPT_DONE_WORD}：成果物が端末のフォルダに置かれたのを確認した。"
RECEIPT_MISSING = f"{RECEIPT_TAG}届いていない：この回は端末のフォルダに成果物が見当たらない。"


@dataclass
class DelegateResult:
    ok: bool = False
    text: str = ""
    receipt: str = ""
    artifacts: list[str] = field(default_factory=list)
    run_id: str = ""
    elapsed: float = 0.0
    error: str = ""
    refused: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "text": self.text, "receipt": self.receipt,
                "artifacts": list(self.artifacts), "run_id": self.run_id,
                "elapsed": round(self.elapsed, 1), "error": self.error,
                "refused": self.refused}


def strip_paths(text: str) -> str:
    """把内部路径从「给模型看的那份」里去掉（给前端的那份不动）。"""
    return _PATH.sub("（端末の中）", str(text or "")).strip()


class DelegateService:
    """一次委任的业务：命名 → 去程 → 转播 → 剥路径 → 回执。"""

    def __init__(self, gateway: DelegateGateway | None, *, artifact_dirs: Iterable[str] = (),
                 wait_s: float = 20.0, deadline: float = 900.0, host: str = "owner",
                 policy=None, log=None) -> None:
        self.gateway = gateway
        self.artifact_dirs = [pathlib.Path(item) for item in artifact_dirs if str(item)]
        self.wait_s = float(wait_s)
        self.deadline = float(deadline)
        self.host = str(host or "owner")
        #: `policy(principal) -> bool`：读 `user.md` 的「权限」段（fail-closed）。
        #: ⚠️ 这是**第二道**：token 那一侧的角色是第一道，两者都过才放行（AND）。
        #: 所以「档里写了允许」不能让 guest 通过，「模型说我可以」更不能。
        self.policy = policy
        self._log = log or (lambda _message: None)
        self._lock = threading.RLock()
        self._done: dict[str, str] = {}          # thread_id → 已委任过的 task（去重）

    # ── 對外 ────────────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return self.gateway is not None and bool(self.gateway.base_url)

    def run(self, *, task: str, principal: str, thread_id: str = "",
            opening: str = "", emit=None, out_name: str = "") -> DelegateResult:
        result = DelegateResult()
        if not self.enabled:
            result.error = "delegate_not_configured"
            return result
        if self.host and str(principal or "") != self.host:
            result.refused = "work_requires_owner"
            self._log(f"delegate: 拒絕（{principal} 不是主人）")
            return result
        if self.policy is not None and not bool(self.policy(principal)):
            # 档里没放开（或没写、写坏）⇒ 拒绝。fail-closed：只可能更严，不可能更松。
            result.refused = "profile_denies_delegate"
            self._log(f"delegate: 拒絕（{principal} 的 user.md 未放开委任）")
            return result
        text = str(task or "").strip()
        if not text:
            result.refused = "empty_task"
            return result
        key = f"{thread_id}::{_norm(text)}"
        with self._lock:
            if key in self._done:
                result.refused = "duplicate_task"
                result.text = RECEIPT_DELIVERED
                result.receipt = RECEIPT_DELIVERED
                self._log(f"delegate: 同一輪重複委任，跳過 thread={thread_id!r}")
                return result
            self._done[key] = result.run_id or "running"

        stem = out_name or self._out_name(thread_id)
        prompt = self._prompt(text, stem)
        before = self._snapshot()

        def on_event(event: str, frame: dict) -> None:
            if emit is None:
                return
            if event == "reasoning.available":
                emit("thinking", {"text": str(frame.get("text") or "")[:400]})
            elif event in {"tool.started", "tool.completed"}:
                emit("progress", {"stage": "delegate", "tool": str(frame.get("tool") or ""),
                                  "state": "started" if event == "tool.started" else "completed",
                                  "preview": strip_paths(str(frame.get("preview") or ""))[:300]})
            elif event.startswith("approval"):
                emit("progress", {"stage": "approval",
                                  "text": str(frame.get("prompt") or frame.get("reason") or "")[:200]})

        outcome: DelegateOutcome = self.gateway.run(
            prompt, session_id=self._session(thread_id), on_event=on_event,
            deadline=self.deadline)
        result.run_id = outcome.run_id
        result.elapsed = outcome.elapsed
        result.error = outcome.error
        result.ok = outcome.ok or (outcome.status == "completed" and not outcome.error)

        artifacts, waited = self._wait_artifacts(stem, before)
        result.artifacts = artifacts
        result.receipt = (RECEIPT_DELIVERED if artifacts else RECEIPT_MISSING) if stem else ""
        body = strip_paths(outcome.text) or "（委任先から文字の返事がなかった）"
        result.text = f"{body}\n\n{result.receipt}".strip() if result.receipt else body
        with self._lock:
            self._done[key] = "done"
        if emit is not None:
            for path in artifacts:
                emit("artifact", {"name": pathlib.Path(path).name, "path": path,
                                  "kind": pathlib.Path(path).suffix.lstrip(".")})
            if result.receipt:
                emit("receipt", {"text": result.receipt, "ok": bool(artifacts)})
        self._log(f"delegate: run={result.run_id} status={outcome.status} "
                  f"{'交付' if artifacts else '无产物'} {outcome.elapsed:.1f}s")
        return result

    # ── 內部 ────────────────────────────────────────────────────────────────
    def _prompt(self, task: str, stem: str) -> str:
        text = task
        if stem:
            text = text.replace("{{OUT}}", stem)
        return text + TAIL

    def _out_name(self, thread_id: str) -> str:
        """约定名：跟着 thread + 时间，模型只需写 `{{OUT}}`（扩展名它自己加）。"""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9_-]+", "", str(thread_id or "")) or "ba"
        return f"ba-{safe}-{stamp}"

    def _session(self, thread_id: str) -> str:
        """一条 thread 对应委任先的一个 session（沿用同一个 session 才有上下文）。"""
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", str(thread_id or "ba"))
        return f"blueagent-next-{safe}" if thread_id else ""

    def _snapshot(self) -> dict[str, float]:
        snap: dict[str, float] = {}
        for root in self.artifact_dirs:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if path.is_file():
                    try:
                        snap[str(path)] = path.stat().st_mtime
                    except OSError:
                        continue
        return snap

    def _wait_artifacts(self, stem: str, before: dict[str, float]) -> tuple[list[str], float]:
        """有界等待 + 目录差分：慢的产线常在 run 收尾之后才把文件落下来。"""
        if not stem or not self.artifact_dirs:
            return [], 0.0
        started = time.monotonic()
        found: list[str] = []
        while True:
            found = []
            for root in self.artifact_dirs:
                if not root.exists():
                    continue
                for path in root.rglob(f"{stem}*"):
                    if path.is_file():
                        found.append(str(path))
            if found:
                return sorted(found), time.monotonic() - started
            if time.monotonic() - started >= self.wait_s:
                return [], time.monotonic() - started
            time.sleep(0.25)

    def forget(self, thread_id: str = "") -> None:
        """新一轮/换话题时清掉去重表（同一句话之后还可以再委任）。"""
        with self._lock:
            if not thread_id:
                self._done.clear()
                return
            for key in [k for k in self._done if k.startswith(f"{thread_id}::")]:
                self._done.pop(key, None)


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


__all__ = ["DelegateResult", "DelegateService", "TAIL", "strip_paths"]
