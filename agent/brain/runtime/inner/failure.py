"""失敗的三分法：**先把「失敗」分類，再決定怎麼辦**（owner 2026-11-03 拍板 ①）。

為什麼要這支：以前一步沒做成只有一個結局——整件事算失敗、停在原地等人。
可是「沒做成」其實是三件不同的事，處置也該不同：

    no_source     環境裡沒有這個東西（查空、頁面打不開、工具全掛）
                  ⇒ 不重試同一條路：換入口，或老實寫「分からなかった」往前走
    wrong_path    這條路本身不通（重複呼叫、步內預算用光、原地打轉）
                  ⇒ 換做法（拆問句、換工具、先做別的小片），並記下「這條走不通」
    goal_invalid  這件事根本不該做／她沒有這個能力（失敗太多次、沒有工作迴路）
                  ⇒ 收工：活動標 FAILED、台帳記一次失敗（`attempts` 到頂就 outdated）

三條規矩：

  ① **判定是程式，不是模型**——模型說「我盡力了」不算，看 trace 與次數；
  ② **分類詞要落盤**（`activity.meta`），重啟後還知道這件是怎麼卡住的；
  ③ **`blocked_routes` 會進下一步的 prompt**——不然下一步照樣往同一個牆上撞。

本模組是純函式（不碰 I/O），好測、好重用。
"""

from __future__ import annotations

#: 三類失敗（順序＝判定優先序：越後面越嚴重）
KINDS = ("no_source", "wrong_path", "goal_invalid")

#: trace 裡這些錯誤代表「呼叫根本沒送出去」⇒ 不是環境沒東西，是她自己的路走錯
_DENIED = frozenset({"duplicate_in_step", "step_call_budget",
                     "not_in_self_work_whitelist", "no_context", "untrusted_input_in_step"})

#: 這些 reason 代表「沒有工作迴路」⇒ 再試一百次也一樣
_NO_WORKER = frozenset({"no_worker", "not_configured"})

#: 這些字眼代表**基礎設施抖了一下**（模型/網路），不是「這條路走不通」，也不該算在她頭上。
#: 2026-11-03 實測：端點不穩時每步都 `call:TimeoutError`，三次就把好好的任務判成 outdated。
_TRANSIENT = ("timeout", "timed out", "connection", "temporarily", "unavailable",
              "502", "503", "504", "reset by peer")


def is_transient(reason: str = "", attempted=()) -> bool:
    """這次失敗是「外面抖了一下」嗎（模型逾時／連不上／5xx）？

    是的話：照實記「這一步沒做成」，但**不累計失敗次數、不升級成收工**——
    不然端點抖三下，她手上那件事就被判死了。
    """
    text = str(reason or "").lower()
    if text.startswith("call:") or any(mark in text for mark in _TRANSIENT):
        return True
    for row in (attempted or ()):
        if not isinstance(row, dict) or row.get("ok"):
            continue
        error = str(row.get("error") or "").lower()
        if any(mark in error for mark in _TRANSIENT):
            return True
    return False


def classify(*, failed: bool, reason: str = "", attempted=(), failures: int = 0,
             has_worker: bool = True, max_attempts: int = 3,
             transient: bool = False) -> str:
    """回 `""`（沒失敗）或三類之一。判定順序：能力 → 次數 → 走錯路 → 環境沒東西。

    `failures` 是**這一次之前**已經失敗幾次（呼叫端的計數），這裡判的是「這一次算進去之後」。
    `transient=True`（模型逾時／連不上）⇒ 一律 `no_source`，不升級。
    """
    if not failed:
        return ""
    if transient:
        return "no_source"
    if not has_worker or str(reason or "") in _NO_WORKER:
        return "goal_invalid"
    if int(failures) + 1 >= max(1, int(max_attempts)):
        return "goal_invalid"
    rows = [row for row in (attempted or ()) if isinstance(row, dict)]
    denied = [row for row in rows
              if str(row.get("error") or "") in _DENIED
              and not row.get("ok")]
    # 有呼叫被規則擋下來（重複／超額）⇒ 這是「路走錯」，不是「世界沒有這個東西」
    if denied and len(denied) == len(rows):
        return "wrong_path"
    if rows:
        return "no_source"
    return "no_source"


def hint_for(kind: str, *, blocked=()) -> str:
    """下一步要帶的「換做法」提示（日文：進 prompt 的文字一律日語）。

    沒有對應處置的類別回空字串（`goal_invalid` 是收工，不給下一步提示）。
    """
    routes = [str(item) for item in (blocked or ()) if str(item).strip()]
    tail = ("\n・もう通らなかった手：" + "／".join(routes[:3])) if routes else ""
    if kind == "wrong_path":
        return ("\n［前回は同じやり方で進まなかった］やり方を変える：問いを分ける／"
                "道具を変える／先に別の小片を片づける。同じ呼び出しは繰り返さない。" + tail)
    if kind == "no_source":
        return ("\n［前回はその手では何も出なかった］入口を変える：別の言葉で引く／"
                "自分のノートを見る／別の資料にする。それでも出なければ"
                "「分からなかった」と書いて次へ進む（無いものを探し続けない）。" + tail)
    return ""


def record(activity, kind: str, *, step_text: str = "", transient: bool = False) -> str:
    """把分類寫進 `activity.meta`，回處置：`"continue"`（這步算過去）或 `"abort"`（收工）。

    落盤欄位：
      · `failure_kind`    這一次的類別（下一次判斷與事後追查都看它）
      · `failures`        這件活動累積失敗幾次（≥2 次時 `_failure_hint` 會要求先寫技能）
      · `blocked_routes`  走不通的路（最多 5 條，去重；會進下一步的 prompt）
      · `abort`           只有 `goal_invalid` 才寫：`{kind, failures, step}`
    """
    meta = getattr(activity, "meta", None)
    if not isinstance(meta, dict):                 # 舊存檔／測試替身：沒有 meta 就當沒有
        return "continue" if kind != "goal_invalid" else "abort"
    meta["failure_kind"] = str(kind)
    if transient:
        # 外面抖了一下：留痕跡，但不記在她頭上（不累計、不進 blocked、不升級）
        meta["transient_failures"] = int(meta.get("transient_failures") or 0) + 1
        return "continue"
    meta["failures"] = int(meta.get("failures") or 0) + 1
    step = str(step_text or "")[:60]
    routes = meta.get("blocked_routes")
    if not isinstance(routes, list):
        routes = []
        meta["blocked_routes"] = routes
    if step and step not in routes and len(routes) < 5:
        routes.append(step)
    if str(kind) == "goal_invalid":
        meta["abort"] = {"kind": str(kind), "failures": int(meta["failures"]), "step": step}
        return "abort"
    return "continue"


def summary(activity) -> str:
    """給日誌／看板的一行（「什麼卡住、卡幾次、哪幾條路走不通」）。"""
    meta = getattr(activity, "meta", None)
    if not isinstance(meta, dict):
        return ""
    kind = str(meta.get("failure_kind") or "")
    if not kind:
        return ""
    routes = [str(item) for item in (meta.get("blocked_routes") or ())]
    return (f"{kind}×{int(meta.get('failures') or 0)}"
            + (f"（{'／'.join(routes[:3])}）" if routes else ""))


__all__ = ["KINDS", "classify", "hint_for", "record", "summary"]
