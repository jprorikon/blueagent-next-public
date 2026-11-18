"""`terminal`：在她**自己的 Ubuntu 虚拟机**里执行命令（owner 2026-11-03）。

为什么不是「容器里开个 shell」：引擎容器是 root，`data/` 里是三个角色共用的记忆库、
台帐、笔记——那样她拿到的是整台引擎。现在每台 VM 是一台**只属于她**的机器：
她自己可以在里面 `apt install`、写脚本、开浏览器，弄坏了重开一台；NAS 与其它角色都不在她视野里。

边界（都是硬编码，不是靠模型自觉）：
  · 目标机 = `data/vm/<entity>.json`（角色 → VM 的 IP／用户名／金钥），模型**不能指定别的机器**
  · 只走 `ssh -i <key> <user>@<ip>`，`-o BatchMode=yes`（不允许交互式密码）
  · 超时 ≤ `TIMEOUT_MAX`（默认 60s，参数可调低）、输出截断 `OUTPUT_CAP`
  · 每一条都写 `data/_ops/shell-<entity>.jsonl`（主人能看到她跑了什么）
  · `modes: work`（聊天里不出现）——聊天要「做」的时候走她自己的活动循环
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import time

from agent.brain.runtime.tools.base import ToolResult

#: 一次命令最多跑多久（秒）
TIMEOUT_MAX = float(os.environ.get("BLUEAGENT_TERM_TIMEOUT_S") or 60.0)
#: 回给模型的输出上限（字节）
OUTPUT_CAP = int(os.environ.get("BLUEAGENT_TERM_OUTPUT_CAP") or 3072)


class TerminalTool:
    """`terminal`：在**她自己的那一台** VM 上跑一条命令。"""

    name = "terminal"
    #: 只在自主工作模式出现（聊天里不给她开 shell）
    modes = ("work",)
    schema = {
        "type": "function",
        "function": {
            "name": "terminal",
            "description": (
                "自分の VM（Ubuntu）でコマンドを一つ実行する。作業ディレクトリはホーム。"
                "自分専用のマシンなので、apt/pip/git での導入も、ファイル作成も、ブラウザの起動も自由。"
                "長い処理は分割する（1 回 60 秒まで・出力 3KB まで）。"
                "NAS や他の二人のデータには触れられない。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "実行するコマンド（1 行）"},
                    "timeout_s": {"type": "integer",
                                  "description": "待つ秒数（既定 20、最大 60）"},
                },
                "required": ["cmd"],
            },
        },
    }

    def __init__(self, vm_root, *, audit_root=None, ssh=None) -> None:
        self.vm_root = pathlib.Path(vm_root)
        self.audit_root = pathlib.Path(audit_root or self.vm_root / ".." / "_ops")
        #: 測試可以換掉（預設真的叫 ssh）
        self._ssh = ssh or self._run_ssh

    # ── 取目標機 ────────────────────────────────────────────────────────────────
    def target(self, entity: str) -> dict | None:
        path = self.vm_root / f"{entity}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not data.get("ip") or not data.get("user") or not data.get("key"):
            return None
        return data

    # ── 執行 ────────────────────────────────────────────────────────────────────
    def _run_ssh(self, target: dict, cmd: str, timeout: float) -> tuple[int, str]:
        # 主機金鑰**第一次見到就記住、之後要一致**（accept-new）：NAT 網段裡別的機器冒充她的 IP
        # 就連不上，而不是把命令和金鑰認證送過去。重建 VM 會換金鑰 ⇒ `vm_fix_ips.sh` 會清掉舊的那行。
        argv = [
            "ssh", "-i", str(target["key"]),
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={self.vm_root / 'known_hosts'}",
            "-o", f"ConnectTimeout=8",
            "-o", "LogLevel=ERROR",
            f'{target["user"]}@{target["ip"]}',
            "--", cmd,
        ]
        started = time.time()
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return -9, f"（{int(timeout)} 秒逾時，已中止）"
        text = (done.stdout or "") + (("\n[stderr]\n" + done.stderr) if done.stderr else "")
        return done.returncode, text

    def run(self, ctx, arguments: dict) -> ToolResult:
        entity = str(getattr(ctx, "entity", "") or "")
        cmd = str((arguments or {}).get("cmd") or "").strip()
        if not cmd:
            return ToolResult(ok=False, error="empty_cmd")
        target = self.target(entity)
        if target is None:
            return ToolResult(ok=False, error="no_vm")
        try:
            timeout = float((arguments or {}).get("timeout_s") or 20)
        except (TypeError, ValueError):
            timeout = 20.0
        timeout = max(1.0, min(TIMEOUT_MAX, timeout))

        started = time.time()
        code, text = self._ssh(target, cmd, timeout)
        ms = int((time.time() - started) * 1000)
        truncated = len(text.encode("utf-8")) > OUTPUT_CAP
        if truncated:
            text = text.encode("utf-8")[:OUTPUT_CAP].decode("utf-8", "ignore")
            text += "\n（出力が長いので切った）"
        self._audit(entity, cmd, code, ms, len(text))
        return ToolResult(ok=code == 0, error="" if code == 0 else f"exit_{code}",
                           data={"exit": code, "ms": ms, "out": text,
                                 "truncated": truncated})

    def _audit(self, entity: str, cmd: str, code: int, ms: int, out_bytes: int) -> None:
        """一行一筆：主人要看「她今天在自己机器上做了什么」靠它。"""
        try:
            self.audit_root.mkdir(parents=True, exist_ok=True)
            path = self.audit_root / f"shell-{entity}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"at": round(time.time(), 3), "entity": entity,
                                         "cmd": cmd[:500], "exit": code, "ms": ms,
                                         "out_bytes": out_bytes}, ensure_ascii=False) + "\n")
        except OSError:
            pass


__all__ = ["TerminalTool"]
