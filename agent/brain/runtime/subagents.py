"""子代理（內在並行思考）——**單行動主線不變**。

owner 2026-10-23：「希望她有子代理功能，在保證現有的單行動主線的情況下，模擬人類的思考」。

所以這裡的子代理**不是**「多幾個會說話的角色」，而是**她腦內的分工**：

    ┌─ recall.scout ─┐   查記憶（事實／窗口摘要／原文），只回證據
    ├─ inner.voice  ─┤   用第一人稱先想一下（第一反應／顧慮），只寫給自己看
    └─ 主線（她）   ─┘   **只有她會說話、只有她能動手**（一次一個動作）

三條硬約束（違反就不是「單一行動主線」了）：

1. **只讀**：子代理拿不到寫入面（不改記憶、不發訊息、不動狀態、不呼叫任何有副作用的工具）。
2. **一層**：子代理不能再開子代理（`depth=1`），避免無限繁殖。
3. **有預算**：每個子代理有 timeout 與字數上限；超時就放棄那個筆記，**不擋主線**。

筆記只進 prompt 的 `[[notes]]` 段（模型自己看），不進對話、不進記憶、不給用戶看。
"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass


@dataclass
class Note:
    agent: str
    text: str
    elapsed_ms: int = 0

    def render(self) -> str:
        return f"[{self.agent}] {self.text}"


class RecallScout:
    """查記憶：只回「有什麼證據」，不下結論。"""

    name = "recall"
    read_only = True
    #: 本地檢索（毫秒級）⇒ 先跑，它的結果要當模型子代理的 evidence
    fast = True

    def __init__(self, search, *, limit: int = 2, max_chars: int = 140) -> None:
        self.search = search
        self.limit = max(1, int(limit))
        self.max_chars = int(max_chars)

    def available(self) -> bool:
        return self.search is not None

    def run(self, *, entity: str, present, text: str, meta=None) -> list[str]:
        if self.search is None or not str(text or "").strip():
            return []
        # 場景跟著回合走（`meta["game"]` 由 SubagentStage 給；沒給＝不限，維持舊呼叫點的行為）
        result = self.search.search(entity=entity, present=present, query=text,
                                    limit=self.limit, game=(meta or {}).get("game"))
        if not result.get("ok"):
            return []
        lines: list[str] = []
        for window in (result.get("windows") or [])[:1]:
            body = f"{window.get('topic') or ''}｜{window.get('summary') or ''}"
            lines.append("前のやりとり：" + body[: self.max_chars])
        for turn in (result.get("turns") or [])[:self.limit]:
            who = turn.get("speaker") or turn.get("role")
            lines.append(f"その時の言葉（{who}）：" + str(turn.get("text") or "")[: self.max_chars])
        for fact in (result.get("facts") or [])[:self.limit]:
            mark = "〔私記〕" if str(fact.get("layer") or "") == "self" else ""
            lines.append("記憶に残っていること：" + mark + str(fact.get("text") or "")[: self.max_chars])
        return lines[: 1 + self.limit * 2]


class InnerVoice:
    """第一人稱的「先想一下」：第一反應＋顧慮。**只寫給自己看**，不許照念。"""

    name = "inner"
    read_only = True

    def __init__(self, call=None, *, max_tokens: int = 160, max_chars: int = 220) -> None:
        self.call = call
        self.max_tokens = int(max_tokens)
        self.max_chars = int(max_chars)

    def available(self) -> bool:
        return self.call is not None

    def run(self, *, entity: str, present, text: str, meta=None) -> list[str]:
        if self.call is None:
            return []
        persona = str((meta or {}).get("persona_hint") or "").strip()
        evidence = "\n".join(str(item) for item in ((meta or {}).get("evidence") or ()))
        prompt = (
            "あなたはある役を演じていて、いま相手の話を聞いている。**まだ返事はしない**。一人称で、今の内側だけを書いて：\n"
            "① 第一の反応（感情、直感）② 引っかかること、言いたくない部分 ③ 聞きたいのにまだ聞けていないこと。\n"
            "二、三文、話し言葉で、箇条書きにしない、相手に話しかけない、台詞を書かない。\n\n"
            f"{('人物：' + persona) if persona else ''}\n"
            f"{('分かっていること：' + chr(10) + evidence) if evidence else ''}\n"
            f"相手が今言ったこと：{text}\n"
        )
        try:
            out = str(self.call(prompt) or "").strip()
        except Exception:                                   # noqa: BLE001 - 子代理失敗不擋主線
            return []
        return [out[: self.max_chars]] if out else []


class SubAgentOrchestrator:
    """按規則決定「這一輪要想哪幾件事」，並把筆記收好。

    規則（刻意簡單、可預測）：
      · 永遠跑 `recall`（除非輸入太短／是純語助詞）
      · `inner` 只在「有情緒含量或需要斟酌」時跑：問句、長句、或 recall 有東西可顧慮
    """

    def __init__(self, agents=(), *, timeout_s: float = 6.0, max_agents: int = 2,
                 enabled: bool = True, max_workers: int = 5,
                 inner_rule: bool = True, inner_min_chars: int = 24) -> None:
        self.agents = [agent for agent in agents if agent is not None]
        self.timeout_s = max(0.5, float(timeout_s))
        #: **這一輪要不要「先想一下」**（docstring 那條規則，到了 2026-11-02 才真的落地）：
        #: 以前 `available()` 只看「有沒有接上 LLM」，於是每輪都跑一次 160 token 的內心戲，
        #: 連「今日なにしてた」也要等它。現在只有問句／長句／記憶裡有東西可顧慮才跑。
        self.inner_rule = bool(inner_rule)
        self.inner_min_chars = max(4, int(inner_min_chars))
        self.max_agents = max(1, int(max_agents))
        #: 同時最多跑幾支（owner 2026-10-24「最大 5 並行」；硬上限 5）
        self.max_workers = max(1, min(5, int(max_workers)))
        self.enabled = bool(enabled)

    def think(self, *, entity: str, present, text: str, meta=None,
              fast_only: bool = False, budget_ms: int | None = None) -> list[Note]:
        """跑這一輪的子代理。**兩段**：

        ① **快的先跑**（`agent.fast`：本地檢索，毫秒級）——它的結果要當後面模型子代理的
           `evidence`，順序不能反。
        ② **其餘並行**（owner 2026-10-24：「最大 5 並行」）。以前是序列跑，
           兩個子代理最壞 2×6 s＝12 s；2026-10-24 實測一輪 14.8 s 裡有 6.4 s 卡在
           「等到 timeout 才回來」的那一支。並行之後總耗時≈最慢那一支。

        `fast_only`：**只跑 `agent.fast`**（App 單人通話，owner 2026-10-29）。
        通話要的是快——LLM 子代理實測 `subagents_ms=3768`，全部卡在她開口之前。
        本地檢索是毫秒級，留著它「想一下」的成本幾乎是零。
        """
        body = str(text or "").strip()
        if not self.enabled or len(body) < 2:
            return []
        # 這一輪最多能給子代理多少毫秒（owner 2026-11-02）：主線的 deadline 是**整輪**的，
        # 子代理只是前置的一段。以前它自己帶 6 s、跟整輪 8 s 各守各的 ⇒ 它「按時成功」跑滿
        # 6 s，主腦就只剩 2 s 出首字（實測 2026-11-02 22:08：subagents 5904ms → brain 38ms）。
        # 只影響**這一輪**（不寫回 self.timeout_s：那是設定值，不是每輪的餘額）。
        budget_s = self.timeout_s if budget_ms is None \
            else min(self.timeout_s, max(0.5, float(budget_ms) / 1000.0))
        wanted = [agent for agent in self.agents if agent.available()]
        if body in {"うん", "はい", "嗯", "哦", "そっか"}:
            wanted = [agent for agent in wanted if agent.name == "recall"]
        # ⚠️ 先濾 fast 再截斷：`max_agents` 截斷會把快的擠掉（`inner` 排在後面不一定，
        #    但別賭排序）。
        if fast_only:
            wanted = [agent for agent in wanted
                      if bool(getattr(agent, "fast", False))]
        wanted = wanted[: self.max_agents]
        fast = [agent for agent in wanted if bool(getattr(agent, "fast", False))]
        rest = [agent for agent in wanted if agent not in fast]
        notes: list[Note] = []
        evidence: list[str] = []
        started = time.time()
        deadline = started + budget_s

        def run_one(agent, payload) -> tuple[list[str], int, str]:
            begin = time.time()
            box: dict = {}

            def worker() -> None:
                try:
                    box["lines"] = list(agent.run(entity=entity, present=present,
                                                  text=body, meta=payload) or [])
                except Exception as exc:                    # noqa: BLE001
                    box["error"] = type(exc).__name__

            thread = threading.Thread(target=worker, daemon=True,
                                      name=f"subagent-{agent.name}")
            thread.start()
            thread.join(max(0.05, deadline - time.time()))
            elapsed = int((time.time() - begin) * 1000)
            if thread.is_alive():
                return [], elapsed, "timeout"                # 不 join，主線不能等它
            if "error" in box:
                return [], elapsed, str(box["error"])
            return [str(line) for line in box.get("lines") or []], elapsed, ""

        def absorb(agent, payload) -> None:
            lines, elapsed, error = run_one(agent, payload)
            if error == "timeout":
                notes.append(Note(agent=agent.name, text="（考えるうちに時間切れ、あきらめた）",
                                  elapsed_ms=elapsed))
                return
            if error:
                notes.append(Note(agent=agent.name, text=f"（失敗：{error}）",
                                  elapsed_ms=elapsed))
                return
            for line in lines:
                text_line = str(line).strip()
                if not text_line:
                    continue
                notes.append(Note(agent=agent.name, text=text_line, elapsed_ms=elapsed))
                evidence.append(text_line)

        # ① 本地快代理（序列，餵 evidence）
        for agent in fast:
            absorb(agent, {**dict(meta or {}), "evidence": list(evidence)})

        # ② 其餘並行（同時最多 `max_workers` 支，硬上限 5）
        #    先問「這一輪需不需要」——`evidence` 是 recall 剛撈回來的，有東西才值得想。
        rest = [agent for agent in rest if self._needs_thinking(agent, body, evidence)]
        if rest and time.time() < deadline:
            workers = min(self.max_workers, len(rest), 5)
            base = {**dict(meta or {}), "evidence": list(evidence)}
            with ThreadPoolExecutor(max_workers=max(1, workers),
                                    thread_name_prefix="subagent-pool") as pool:
                futures = [(agent, pool.submit(run_one, agent, dict(base)))
                           for agent in rest]
                for agent, future in futures:               # 照提交順序收，輸出可重現
                    try:
                        lines, elapsed, error = future.result(
                            timeout=max(0.05, deadline - time.time()))
                    except FutureTimeout:
                        notes.append(Note(agent=agent.name, text="（考えるうちに時間切れ、あきらめた）",
                                          elapsed_ms=int((time.time() - started) * 1000)))
                        continue
                    if error == "timeout":
                        notes.append(Note(agent=agent.name, text="（考えるうちに時間切れ、あきらめた）",
                                          elapsed_ms=elapsed))
                        continue
                    if error:
                        notes.append(Note(agent=agent.name, text=f"（失敗：{error}）",
                                          elapsed_ms=elapsed))
                        continue
                    for line in lines:
                        text_line = str(line).strip()
                        if not text_line:
                            continue
                        notes.append(Note(agent=agent.name, text=text_line,
                                          elapsed_ms=elapsed))
                        evidence.append(text_line)
        self.last_elapsed_ms = int((time.time() - started) * 1000)
        return notes
    #: 問句的尾巴（日文常省略問號，所以看句尾）與疑問詞
    _QUESTION_TAILS = ("か", "の", "かな", "?", "？")
    _QUESTION_WORDS = ("どう", "なに", "何", "どこ", "いつ", "だれ", "誰", "なんで",  # lang-ok: 比對詞
                       "为什么", "怎麼", "怎么", "吗", "嗎")                        # lang-ok: 比對詞

    def _needs_thinking(self, agent, body: str, evidence) -> bool:
        """這一輪需不需要那個 LLM 子代理（`inner`）：問句／長句／記憶裡有東西。

        ⚠️ **只裁 `inner`**：其他子代理（現在沒有，但以後會加）與呼叫端自己塞進來的
        假代理都不該被這條規則吃掉——`name` 對不上就一律照跑。
        """
        if not self.inner_rule or str(getattr(agent, "name", "")) != "inner":
            return True
        if evidence:
            return True
        text = str(body or "").strip()
        if len(text) >= self.inner_min_chars:
            return True
        if text.rstrip("。！!…　 ").endswith(self._QUESTION_TAILS):
            return True
        return any(word in text for word in self._QUESTION_WORDS)


class ProgressCritic:
    """位移判定（owner 2026-11-03 ④，來自外部提案的 Critic／熔斷器）。

    她是自己做事，沒有人在中間驗收——於是很容易「每一步都寫得像做完了，其實在原地繞」。
    這個只讀的觀察者只判一件事：**這一步比上一步有沒有往前**。

    三條硬約束（跟其他子代理一致，見本檔開頭）：
      · **只讀**：不改記憶、不發訊息、不動狀態（判定結果由呼叫端自己寫進 `activity.meta`）；
      · **不擋主線**：3 秒拿不到答案就放棄，這一拍當「判不了」⇒ **放行**（誤殺比放過更糟）；
      · **不說話**：判定只有 `moved`／`why` 兩個欄位，不產生台詞。

    呼叫端（`runtime/life_step.py`）用連續 3 次 `moved=false` 觸發「換做法」，6 次就收工。
    """

    name = "critic"
    read_only = True

    PROMPT = (
        "あなたは、ある人物が自分でやっている仕事を外から見ている観察者。"
        "判定するのは一点だけ：この一歩は、前の一歩より**先に進んだか**。\n"
        "進んだ（yes）：新しい事実が分かった／成果物が増えた・具体になった／"
        "入口や手段が変わって前へ動いた／決着がついた。\n"
        "進んでいない（no）：同じことを言い直しただけ／同じ調べ方を繰り返した／"
        "感想や決意だけで中身が無い／さっきと同じ結論に戻った。\n"
        "出力は JSON だけ：{{\"moved\": \"yes\" か \"no\", \"why\": \"一行（日本語）\"}}\n\n"
        "活動：{activity}\nこの一歩：{step}\n出てきた中身：{result}\n"
        "これまでの判定：{marks}\n"
    )

    def __init__(self, call=None, *, timeout_s: float = 3.0, max_chars: int = 300) -> None:
        self.call = call
        self.timeout_s = max(0.2, float(timeout_s))
        self.max_chars = int(max_chars)
        self.reviews = 0
        self.failures = 0

    def available(self) -> bool:
        return self.call is not None

    def _ask(self, prompt: str) -> str:
        return str(self.call(prompt) or "")

    def review(self, *, activity: str, step: str, result: str, marks=()) -> dict:
        """回 `{"moved": bool, "why": str, "elapsed_ms": int}`。

        **判不了就放行**（`moved=True`）：觀察者壞掉不該讓她停下自己的工作。
        """
        if self.call is None:
            return {"moved": True, "why": "", "elapsed_ms": 0, "skipped": True}
        history = "；".join(str(item)[:60] for item in (marks or ()) if str(item).strip())
        prompt = self.PROMPT.format(activity=str(activity)[:120], step=str(step)[:120],
                                    result=str(result)[:self.max_chars],
                                    marks=history or "（まだ）")
        started = time.time()
        # ⚠️ 不能用 `with ThreadPoolExecutor(...)`：離場時 shutdown(wait=True) 會**等那顆跑完**，
        #    3 秒的超時就等於沒有（2026-11-03 實測 0.6 秒的假 call 拖到 0.6 秒才回來）。
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="critic")
        try:
            out = pool.submit(self._ask, prompt).result(timeout=self.timeout_s)
        except Exception:                              # noqa: BLE001 - 觀察者壞了不擋主線
            self.failures += 1
            return {"moved": True, "why": "", "elapsed_ms": int((time.time() - started) * 1000),
                    "error": "timeout"}
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        self.reviews += 1
        verdict = _parse_verdict(out)
        verdict["elapsed_ms"] = int((time.time() - started) * 1000)
        return verdict


def _parse_verdict(raw: str) -> dict:
    """從回覆裡抓 `moved`；抓不到就當**判不了**（放行，不當成「沒進步」）。"""
    body = str(raw or "").strip()
    # 先當 JSON 讀（模型常包在 ```json 圍欄裡；也順手處理 \u 轉義）
    candidate = body
    if "```" in candidate:
        parts = candidate.split("```")
        candidate = max(parts, key=len).strip()
        if candidate.lower().startswith("json"):
            candidate = candidate[4:].strip()
    try:
        data = json.loads(candidate)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict) and "moved" in data:
        moved = str(data.get("moved") or "").strip().lower()
        return {"moved": moved in {"yes", "true", "1", "y", "はい", "進んだ"},
                "why": str(data.get("why") or "")[:80]}
    low = body.lower()
    if '"moved"' in low or "'moved'" in low:
        for token, moved in (('"yes"', True), ("'yes'", True),
                             ('"no"', False), ("'no'", False)):
            if token in low:
                why = ""
                for key in ('"why"', "'why'"):
                    index = low.find(key)
                    if index >= 0:
                        tail = body[index:index + 160]
                        why = tail.split(":", 1)[-1].strip().strip('"} \'')
                        break
                return {"moved": moved, "why": why[:80]}
    if re.search(r"\byes\b", low) and not re.search(r"\bno\b", low):
        return {"moved": True, "why": ""}
    if re.search(r"\bno\b", low) and not re.search(r"\byes\b", low):
        return {"moved": False, "why": ""}
    return {"moved": True, "why": "unparsed"}


__all__ = ["Note", "RecallScout", "InnerVoice", "ProgressCritic", "SubAgentOrchestrator"]
