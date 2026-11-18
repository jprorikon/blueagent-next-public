"""存摺（RP 帳本）：**只記帳，不擋人**（owner 2026-11-01 定）。

分工（aster 2026-11-01 的結論，逐字照收）：
- **閘**留在 `runtime/budget.py`（三分檔：心跳累積／自主消耗／他人發起不受限）；
- **帳**是這一支：append-only 流水，回答「還有多少、什麼時候花的、誰扣的」，**不參與任何能不能做的判斷**。
  aster 原話：「RP 只買沒人叫你做的事，閘留在進場那一層，兩套永遠不會打架。」

owner 2026-11-01 的四個決定：
1. 匯率照 aster：**0.1 RP／5 分鐘**（＝1.2 RP／小時；內部用整數 mrp，1 RP ＝ 1000 mrp）
2. **不設到期**（aster 那邊有 3 天 TTL；我們不做）
3. 單位：我們的「點」就是 RP（1 點 ＝ 1 RP；內部仍存 mrp 整數）
4. **他人發起不記帳**（只記她自己的事）

設計要點（每一條都是 aster 那邊被 19 個失敗案例打出來的，連理由一起抄）：
- **整數運算**（mrp）：一天幾百筆，浮點會歪。
- **`flock`**：心跳與別的行程同時寫會撞號。
- **崩尾不丟**：最後一行半截 ⇒ 搬到 `rp.jsonl.torn-<epoch>` 再截回，其餘照用；中間壞行跳過並計數。
- **冪等**：`src` 唯一（`tick:<5 分鐘格>`、`activity:<id>`），重複入帳回 `duplicate` 而不是加兩次。
- **`correct` 更正單**：不改舊行，追加一筆；可以指向 deposit／spend，不指向 correct／expire。
- **狀態只從流水算**：`status()` 是唯一權威，面板／UI 一律讀它、**不自算**。
  （aster 的實例：面板自算漏了 `correct` 與到期 ⇒ 顯示 36.080 RP，實帳 0.480。）
"""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
import re
import time

#: 一點／一 RP ＝ 多少 mrp（千分之一 RP）
MRP_PER_RP = 1000
#: 入帳：每 5 分鐘 0.1 RP（aster 的匯率；owner 2026-11-01 拍板照用）
TICK_S = 300
RATE_MRP = 100
KINDS = ("deposit", "spend", "correct", "expire")


def _slug(entity: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", str(entity or "").lower()).strip("-") or "unknown"


def _rp(mrp: int) -> float:
    return round(int(mrp) / MRP_PER_RP, 3)


class RpLedger:
    """一個角色一本存摺（`<root>/<角色>.jsonl`）。所有寫入走同一把 `flock`。"""

    def __init__(self, root, *, clock=time.time, log=None,
                 tick_s: int = TICK_S, rate_mrp: int = RATE_MRP) -> None:
        self.root = pathlib.Path(root)
        self.tick_s = max(1, int(tick_s))
        self.rate_mrp = max(0, int(rate_mrp))
        self._clock = clock
        self._log = log or (lambda _message: None)
        self._cache: dict[str, dict] = {}

    # ── 路徑 ────────────────────────────────────────────────────────────────
    def path(self, entity: str) -> pathlib.Path:
        return self.root / f"{_slug(entity)}.jsonl"

    def _lock_path(self, entity: str) -> pathlib.Path:
        return self.root / f"{_slug(entity)}.lock"

    # ── 讀（含崩尾處理）────────────────────────────────────────────────────
    def _read_rows(self, entity: str) -> list[dict]:
        path = self.path(entity)
        try:
            raw = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return []
        lines = raw.split("\n")
        tail = lines.pop() if lines else ""
        rows: list[dict] = []
        bad = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(row, dict) and isinstance(row.get("seq"), int):
                rows.append(row)
        if tail.strip():
            # 最後一行沒有換行 ⇒ 多半是崩在寫入中間：搬去 .torn-<epoch>，把檔案截回好行
            try:
                row = json.loads(tail)
                if isinstance(row, dict) and isinstance(row.get("seq"), int):
                    rows.append(row)                     # 其實是完整的，只是沒有尾換行
                    with path.open("a", encoding="utf-8") as stream:
                        stream.write("\n")
                else:
                    raise ValueError("bad row")
            except ValueError:
                self._park_torn(entity, tail)
        if bad:
            self._log(f"rp[{entity}]: 跳過 {bad} 行壞行（不動它們）")
        return rows

    def _park_torn(self, entity: str, tail: str) -> None:
        path = self.path(entity)
        torn = path.with_name(f"{path.name}.torn-{int(self._clock())}")
        try:
            torn.write_text(tail, encoding="utf-8")
            good = path.read_text(encoding="utf-8").rpartition("\n")[0]
            path.write_text(good + "\n" if good else "", encoding="utf-8")
            self._log(f"rp[{entity}]: 崩尾搬到 {torn.name}")
        except OSError as exc:
            self._log(f"rp[{entity}]: 崩尾處理失敗（{type(exc).__name__}）")

    # ── 算（唯一權威）──────────────────────────────────────────────────────
    def status(self, entity: str) -> dict:
        """從流水重算一次（不信任任何快取）；回餘額、各類合計、最後序號。"""
        rows = self._read_rows(entity)
        balance = 0
        by_kind = {kind: 0 for kind in KINDS}
        srcs = set()
        last_at = 0.0
        for row in rows:
            amount = int(row.get("amount_mrp") or 0)
            kind = str(row.get("kind") or "")
            balance += amount
            if kind in by_kind:
                by_kind[kind] += amount
            src = str(row.get("src") or "")
            if src:
                srcs.add(src)
            last_at = max(last_at, float(row.get("at") or 0.0))
        state = {
            "entity": str(entity), "entries": len(rows),
            "last_seq": int(rows[-1]["seq"]) if rows else 0,
            "balance_mrp": int(balance), "balance_rp": _rp(balance),
            "overdraft_rp": _rp(max(0, -balance)),
            "by_kind_mrp": by_kind,
            "last_at": last_at, "_srcs": srcs,
        }
        self._cache[_slug(entity)] = state
        return state

    def balance(self, entity: str) -> float:
        return float(self.status(entity)["balance_rp"])

    # ── 寫（全部走同一把鎖）────────────────────────────────────────────────
    def _append(self, entity: str, kind: str, amount_mrp: int, src: str, note: str) -> dict:
        kind = str(kind or "")
        if kind not in KINDS:
            return {"ok": False, "error": "BAD_KIND"}
        path = self.path(entity)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = self._lock_path(entity)
        with open(lock, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                state = self.status(entity)
                if src and src in state["_srcs"]:
                    return {"ok": True, "duplicate": True, "src": src,
                            "balance_rp": state["balance_rp"]}
                row = {"seq": int(state["last_seq"]) + 1, "at": round(float(self._clock()), 3),
                       "kind": kind, "amount_mrp": int(amount_mrp), "src": str(src or ""),
                       "note": str(note or "")[:160]}
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        after = self.status(entity)
        return {"ok": True, "seq": row["seq"], "balance_rp": after["balance_rp"], "row": row}

    # ── 對外 ────────────────────────────────────────────────────────────────
    def deposit(self, entity: str, amount_mrp: int, *, src: str = "", note: str = "") -> dict:
        return self._append(entity, "deposit", int(amount_mrp), src, note)

    def deposit_tick(self, entity: str, *, at: float | None = None) -> dict:
        """心跳／任何 tick 都會叫：同一格（5 分鐘）只入帳一次，所以叫幾次都安全。"""
        if self.rate_mrp <= 0:
            return {"ok": True, "skipped": "rate_zero"}
        now = float(at if at is not None else self._clock())
        grid = int(now // self.tick_s)
        return self._append(entity, "deposit", self.rate_mrp, f"tick:{grid}",
                             f"每 {self.tick_s} 秒 {_rp(self.rate_mrp)} RP")

    def spend(self, entity: str, points: float, *, src: str = "", note: str = "") -> dict:
        """扣錢（**只記帳**，不擋任何事）。`points` 用我們既有的「點」＝RP。"""
        mrp = int(round(float(points) * MRP_PER_RP))
        return self._append(entity, "spend", -abs(mrp), src, note)

    def correct(self, entity: str, target_seq: int, amount_mrp: int, *, note: str = "") -> dict:
        """更正單：追加一筆 delta，不改舊行；不可指向 `correct`／`expire`。"""
        rows = {int(row["seq"]): row for row in self._read_rows(entity)}
        target = rows.get(int(target_seq))
        if target is None:
            return {"ok": False, "error": "TARGET_NOT_FOUND"}
        if str(target.get("kind")) not in {"deposit", "spend"}:
            return {"ok": False, "error": "TARGET_NOT_CORRECTABLE"}
        return self._append(entity, "correct", int(amount_mrp), "",
                            f"correct seq={int(target_seq)} {note}".strip())


class LedgerMirror:
    """把既有的預算（＝**閘**）鏡射到存摺（＝**帳**）：閘的判斷一個字都不改，帳多記一筆。

    為什麼用鏡射而不是在每個呼叫點加記帳：呼叫點有四個（心跳累積、LifeLoop 開工、
    步內網路費、手動活動），漏一個帳就有洞。鏡射只有一個接縫，介面其餘部分照舊轉給原物件。
    """

    def __init__(self, budget, ledger, *, clock=time.time) -> None:
        self._budget = budget
        self._ledger = ledger
        self._clock = clock

    def __getattr__(self, name):
        return getattr(self._budget, name)

    def accrue(self, *args, **kwargs):
        entity = args[0] if args else kwargs.get("entity")
        result = self._budget.accrue(*args, **kwargs) if hasattr(self._budget, "accrue") else None
        if entity:
            try:
                self._ledger.deposit_tick(str(entity), at=self._clock())
            except Exception as exc:                      # noqa: BLE001 - 記帳不能擋累積
                self._ledger._log(f"rp: 入帳失敗（{type(exc).__name__}）")
        return result

    def try_spend(self, entity, cost, **kwargs):
        src = str(kwargs.pop("src", "") or "")
        result = self._budget.try_spend(entity, cost, **kwargs)
        try:
            self._ledger.spend(str(entity), cost, src=src,
                               note=str(kwargs.get("reason") or ""))
        except Exception as exc:                          # noqa: BLE001
            self._ledger._log(f"rp: 扣帳失敗（{type(exc).__name__}）")
        return result

    def charge(self, entity, cost, **kwargs):
        src = str(kwargs.pop("src", "") or "")
        charge = getattr(self._budget, "charge", None)
        result = charge(entity, cost, **kwargs) if charge is not None else None
        try:
            self._ledger.spend(str(entity), cost, src=src,
                               note=str(kwargs.get("reason") or ""))
        except Exception as exc:                          # noqa: BLE001
            self._ledger._log(f"rp: 扣帳失敗（{type(exc).__name__}）")
        return result


def selftest() -> dict:
    """`python3 -m brain.runtime.inner.rp_ledger selftest`：真跑一遍，不是宣稱。"""
    import tempfile

    passed = failed = 0
    checks: list[tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        nonlocal passed, failed
        checks.append((name, bool(ok)))
        passed += bool(ok)
        failed += not ok

    clock = [1000.0]
    root_dir = tempfile.mkdtemp(prefix="rp-selftest-")
    book = RpLedger(root_dir, clock=lambda: clock[0])

    # ① 入帳：同一格冪等
    a = book.deposit_tick("mira")
    clock[0] += 60
    b = book.deposit_tick("mira")
    check("同一個 5 分鐘格只入帳一次", b.get("duplicate") and book.status("mira")["balance_mrp"] == RATE_MRP)
    clock[0] += 300
    book.deposit_tick("mira")
    check("下一格再入一筆", book.status("mira")["balance_mrp"] == 2 * RATE_MRP)

    # ② 扣款：整數、可負（帳不是閘）。兩個 tick ＝ 0.2 RP
    book.spend("mira", 1.0, note="study")
    check("0.2 RP 的存底扣掉 1 RP ⇒ -0.8", book.status("mira")["balance_rp"] == -0.8)
    book.spend("mira", 2.5, note="excursion")
    check("可以變成負的（帳不是閘）", book.status("mira")["balance_rp"] == -3.3)

    # ③ 冪等：同一個 src 不會扣兩次
    book.spend("mira", 1.0, src="activity:x1")
    book.spend("mira", 1.0, src="activity:x1")
    check("同一個 src 只記一次", book.status("mira")["balance_rp"] == -4.3)

    # ④ 更正：只准指向 deposit／spend，且是追加（不改舊行）
    book.correct("mira", 2, 500, note="補記")
    check("更正 deposit ⇒ 餘額 +0.5", book.status("mira")["balance_rp"] == -3.8)
    bad_seq = book.status("mira")["last_seq"]
    bad = book.correct("mira", bad_seq, 100)
    check("不能更正『更正』本身", bad.get("error") == "TARGET_NOT_CORRECTABLE")
    check("更正沒有改寫舊行（1..6 連續）",
          [int(row["seq"]) for row in book._read_rows("mira")] == [1, 2, 3, 4, 5, 6])

    # ⑤ 崩尾：最後一行半截 ⇒ 搬到 .torn，其餘照用
    path = book.path("mira")
    good = book.status("mira")["balance_mrp"]
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"seq": 999, "at": 1.0, "kind": "spend", "amount_')
    after = book.status("mira")
    check("崩尾不影響已記的帳", after["balance_mrp"] == good)
    check("崩尾有留檔（.torn-*）", any(p.name.startswith(path.name + ".torn-")
                                       for p in pathlib.Path(root_dir).iterdir()))

    # ⑥ 壞行不炸
    with path.open("a", encoding="utf-8") as stream:
        stream.write("這不是 JSON\n")
    check("中間壞行跳過、不炸", book.status("mira")["balance_mrp"] == good)

    # ⑦ 兩個 writer 併發不會撞號
    import threading
    book2 = RpLedger(root_dir, clock=lambda: clock[0])
    threads = [threading.Thread(target=lambda n=n: [book2.spend("mira", 0.1, src=f"t{n}-{i}")
                                                    for i in range(5)]) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    seqs = [int(row["seq"]) for row in book2._read_rows("mira")]
    check("併發寫入序號不重複", len(seqs) == len(set(seqs)))

    return {"passed": passed, "failed": failed, "checks": checks}


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        result = selftest()
        for name, ok in result["checks"]:
            print(("PASS " if ok else "FAIL ") + name)
        print(f"{result['passed']}/{result['passed'] + result['failed']} PASS")
        raise SystemExit(0 if not result["failed"] else 1)
    print("用法：python3 -m brain.runtime.inner.rp_ledger selftest")


__all__ = ["MRP_PER_RP", "RATE_MRP", "RpLedger", "TICK_S", "selftest"]
