"""出口閘：她**先開口**的那一句，主人只憑他看得到的東西能不能看懂（common-ground 第 5、6 步）。

docs/PLAN-common-ground-20261109.md §4。她開口的動機（獨白、世界層、私人活動、到期預約）主人一句都沒看過，
所以送出前換到主人的位置檢查一次——**只給檢查者主人看得到的東西**：

  ① 規則層（便宜、必跑）：草稿裡有指示詞（「例の」「さっきの」「前に言った」「見た？」…），
     而主人那邊**最近沒有可見的對話**可以讓它指 ⇒ 這是懸空的指代，打回重寫一次。
  ② 評審層（一次小呼叫，可關）：只給「主人這邊最近看得到的幾則」＋草稿，問「只憑這些看得懂嗎？缺什麼？」
     看不懂 ⇒ 帶著缺的那句重寫一次。
  ③ 重複（owner 2026-11-09）：主人還沒回的前一條（`previous`）跟草稿是同一件事 ⇒ 重寫一次，
     要她寫出「知道自己已經說過」的一句，或換個話題。
  重寫後規則層再看一次；還懸空／還重複 ⇒ **不發**（沉默是合法結果）。
  評審判不了（超時／解析不到／壞了）⇒ 放行（同 `ProgressCritic`：誤殺比放過更糟）。

只做判定與重寫的流程，不知道「主人看得到什麼」怎麼取（呼叫端給 `visible`），也不送訊息。
"""

from __future__ import annotations

import json
import os
import re
import threading

#: 會懸空的指示說法（寧可少收：「あれ？」這種驚訝的感嘆不收）
DEIXIS: tuple[str, ...] = (
    "例の", "さっきの", "さっき言った", "さっき送った", "前に言った", "前に話した", "この前の",
    "あの件", "あの話", "あのこと", "あれって", "あれ見た", "あれ、どう", "あれどう",
    "続きだけど", "の続き", "見た？", "見てくれた", "読んだ？", "読んでくれた", "どうだった？",
)

CRITIC_PROMPT = """あなたは受け手（オーナー）の立場で読む係。オーナーに見えているのは、下の「オーナーに見えている会話」だけ。
相手の頭の中、相手が一人でしていたこと、相手が調べたことは、オーナーには見えていない。

オーナーに見えている会話（古い→新しい）：
{visible}

いま相手から届いた一言：
「{draft}」
{previous}
この一言だけで、オーナーは何の話か分かる？ 分からない前提（指しているものが見えていない、急に知らない話が出る）があれば書く。
前に送ってまだ返事のない一言と同じ用件の繰り返し（言い換えも含む）なら、それも ok=false にする。
出力は JSON 一行だけ：{{"ok": true か false, "missing": "足りない前提（20字以内、ok なら空）"}}"""


def dangling(text: str) -> list[str]:
    """草稿裡的指示說法（不看語境；懸不懸空由呼叫端看「最近有沒有可見的對話」）。"""
    body = str(text or "")
    return [word for word in DEIXIS if word in body]


#: 跟「還沒回的前一條」二字重疊到這個比例就算同一件事（與 `ProactiveGate` 的硬擋同值）
REPEAT_OVERLAP = 0.5


#: 比重複時先拿掉的稱呼與口頭禪（每句都會有，留著會讓兩句不相干的話看起來很像）
_FILLERS = ("オーナー", "オーナー", "ねえ", "あのね", "ちょっと", "なんか")


def _grams(text: str) -> set[str]:
    body = str(text or "")
    for word in _FILLERS:
        body = body.replace(word, "")
    body = "".join(ch for ch in body if ch.isalnum())
    return {body[i:i + 2] for i in range(len(body) - 1)} if len(body) > 1 else ({body} if body else set())


def repeats(text: str, previous) -> bool:
    """草稿是不是在重複主人還沒回的那幾條：二字重疊佔**較短那句**的 ≥0.5（換句話說、加長也算）。

    以「較短那句」為分母：「観測表、できたよ」→「観測表ができたから、見てね」只多了幾個字，
    用新句當分母只有 0.36，看起來像別的事；用短句當分母是 0.67，才認得出是同一件事。
    """
    wanted = _grams(text)
    if not wanted:
        return False
    for prior in previous or ():
        other = _grams(prior)
        if other and len(wanted & other) / min(len(wanted), len(other)) >= REPEAT_OVERLAP:
            return True
    return False


def _parse(raw: str) -> dict | None:
    match = re.search(r"\{.*\}", str(raw or ""), re.S)
    if not match:
        return None
    try:
        body = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(body, dict) or "ok" not in body:
        return None
    ok = body.get("ok")
    if isinstance(ok, str):
        ok = ok.strip().lower() in {"true", "yes", "1"}
    return {"ok": bool(ok), "missing": str(body.get("missing") or "").strip()[:60]}


def _env_on(name: str, default: str = "1") -> bool:
    return str(os.environ.get(name) or default).strip().lower() not in {"0", "off", "false", "no"}


class PremiseCheck:
    """`review(draft, visible=…, redo=…)` → `{text, verdict, missing, rewrote}`；`text` 空＝不發。"""

    def __init__(self, *, critic=None, timeout_s: float | None = None, log=None,
                 enabled: bool | None = None, critic_enabled: bool | None = None) -> None:
        #: `critic(prompt) -> str`：評審層的小呼叫；沒有 ⇒ 只跑規則層
        self.critic = critic
        self.timeout_s = float(timeout_s if timeout_s is not None
                               else os.environ.get("BLUEAGENT_PREMISE_TIMEOUT_S") or 6.0)
        self.enabled = _env_on("BLUEAGENT_PREMISE_CHECK") if enabled is None else bool(enabled)
        self.critic_enabled = _env_on("BLUEAGENT_PREMISE_CRITIC") if critic_enabled is None \
            else bool(critic_enabled)
        self._log = log or (lambda _message: None)
        self.stats = {"pass": 0, "rewrote": 0, "dropped": 0, "critic_errors": 0}

    # ── 評審層 ──────────────────────────────────────────────────────────────
    def _ask(self, draft: str, visible: list[str], previous=()) -> dict | None:
        """只給主人看得到的東西；判不了回 None（放行）。"""
        if self.critic is None or not self.critic_enabled:
            return None
        earlier = "".join(f"\n前に送って、まだ返事のない一言：「{str(row)[:120]}」" for row in previous or ())
        prompt = CRITIC_PROMPT.format(visible="\n".join(visible) or "（まだ何もない）",
                                      draft=str(draft)[:300], previous=earlier)
        box: dict = {}

        def run() -> None:
            try:
                box["raw"] = self.critic(prompt)
            except Exception as exc:                     # noqa: BLE001
                box["error"] = exc

        worker = threading.Thread(target=run, daemon=True, name="premise-critic")
        worker.start()
        worker.join(self.timeout_s)
        if worker.is_alive() or "error" in box:
            self.stats["critic_errors"] += 1
            return None
        return _parse(box.get("raw") or "")

    # ── 一次審 ──────────────────────────────────────────────────────────────
    def review(self, draft: str, *, visible=(), fresh: bool = False, redo=None,
               previous=()) -> dict:
        """`visible`：主人那邊最近看得到的幾則（已經是人話的行）；`fresh`：最近有沒有可見的對話。

        `redo(hint) -> str`：帶著「缺什麼」重寫一次（沒有就不能重寫 ⇒ 懸空就不發）。
        `previous`：主人還沒回的那幾條（原文）——草稿不能是它們的重複。
        """
        text = str(draft or "").strip()
        if not self.enabled or not text:
            return {"text": text, "verdict": "off" if not self.enabled else "empty",
                    "missing": "", "rewrote": False}
        lines = [str(line) for line in (visible or ()) if str(line).strip()]
        earlier = [str(row) for row in (previous or ()) if str(row).strip()]
        hint = ""
        hits = [] if fresh else dangling(text)
        if hits:
            hint = "指しているもの（" + "・".join(hits[:3]) + "）がオーナーには見えていない"
        elif repeats(text, earlier):
            hint = "前に送ってまだ返事のない一言と同じ用件になっている"
        else:
            verdict = self._ask(text, lines, earlier)
            if verdict is not None and not verdict["ok"]:
                hint = verdict["missing"] or "何の話かが見えていない"
        if not hint:
            self.stats["pass"] += 1
            return {"text": text, "verdict": "pass", "missing": "", "rewrote": False}
        again = ""
        if redo is not None:
            try:
                again = str(redo(hint) or "").strip()
            except Exception as exc:                     # noqa: BLE001 - 重寫壞了 ⇒ 不發
                self._log(f"premise: 重寫失敗（{type(exc).__name__}）")
        if again and (fresh or not dangling(again)) and not repeats(again, earlier):
            self.stats["rewrote"] += 1
            return {"text": again, "verdict": "rewrote", "missing": hint, "rewrote": True}
        self.stats["dropped"] += 1
        self._log(f"premise: 看不懂就不發（{hint}）")
        return {"text": "", "verdict": "dropped", "missing": hint, "rewrote": bool(again)}


__all__ = ["CRITIC_PROMPT", "DEIXIS", "PremiseCheck", "REPEAT_OVERLAP", "dangling", "repeats"]
