"""她「自己的事」也會改變心情（owner 2026-10-22：「她的個人活動也應該影響心情變化」）。

原則：
  · **按活動類型**給增量，不是「做完一件事就爽」（跟遊戲事件同一套思路）；
  · 文本裡出現卡住／失敗／放棄時，改成挫折；
  · 記帳對象是 **她自己**（`target_type="self"`），不是主人。
"""

from __future__ import annotations

#: kind → (delta_valence, delta_arousal, AEM 種類)
ACTIVITY_AFFECT: dict[str, tuple[float, float, str]] = {
    "study":    (+0.04, +0.03, "interest"),
    "practice": (+0.05, +0.05, "pleasure"),
    "play":     (+0.06, +0.06, "pleasure"),
    "explore":  (+0.05, +0.07, "interest"),
    "journal":  (+0.03, -0.02, "pleasure"),
    "organize": (+0.02, -0.01, "pleasure"),
    "review":   (+0.02,  0.00, "interest"),
    "ambient":  (+0.01,  0.00, "pleasure"),
}

#: 這些字眼出現 ⇒ 這一件事不順（改成挫折，蓋掉上面那條）
FRUSTRATION_MARKS = ("卡住", "失敗", "放棄", "做不完", "搞不懂", "被卡")


def appraise_activity(affect, entity: str, kind: str, text: str = "",
                      *, source: str = "", finished: bool = True) -> dict:
    """把「自己的事」記進 AEM。回 `{ok, appraisal, delta}`。

    **一件事收尾才記一次**（`finished`）；中途的步驟只有「卡住／失敗」才記（挫折要即時）。
    為什麼（情感整理 2026-11-09）：核心的 valence 是加權平均，每筆在分子上貢獻 2·Δv——
    每一步都記的話，她忙一個下午的瑣事會累積成跟「被主人拒絕」同量級的推力，蓋掉主人那邊的事。
    """
    if affect is None:
        return {"ok": False, "reason": "no_affect"}
    body = str(text or "")
    hit = next((mark for mark in FRUSTRATION_MARKS if mark in body), "")
    if not hit and not finished:
        return {"ok": True, "skipped": "step", "kind": str(kind or "ambient")}
    if hit:
        appraisal, dv, da = "frustration", -0.05, +0.05
    else:
        dv, da, appraisal = ACTIVITY_AFFECT.get(str(kind or "ambient"), (0.02, 0.0, "pleasure"))
    # 走核心的 `outcome`（她自己的事）：以前走 `appraise()` 一律 SOURCE_NOT_FOUND，
    # 還把失敗回報成 ok——她的活動從來沒進過心情（情感整理 2026-11-09）。
    try:
        result = affect.outcome(str(entity), kind=appraisal, delta_v=dv, delta_a=da,
                                event_id=f"act:{entity}:{kind}:{abs(hash(body)) % 10**8}",
                                target_type="task", target_id=str(entity))
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    if not (result or {}).get("ok"):
        return {"ok": False, "reason": str((result or {}).get("error") or "rejected")}
    return {"ok": True, "appraisal": appraisal, "delta": [dv, da],
            "kind": str(kind or "ambient"), "mark": hit}


__all__ = ["ACTIVITY_AFFECT", "FRUSTRATION_MARKS", "appraise_activity"]
