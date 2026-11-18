"""開下一件自己的事之前，**她可以選擇不做**（owner 2026-11-09「心情不好，就不做了」）。

以前生活迴圈只在這幾種時候不開新的：睡著、睡前一小時／醒後半小時、體力不夠、有人在說話、
輪換排不出來。心情完全不參與——低落的時候照樣一件接一件。

規則（只看心情，不調模型）：

  · しょんぼり ⇒ 一半的機會先不做；いらいら ⇒ 三成；
  · 有點低落（valence ≤ −0.1）又沒什麼精神（arousal < 0.08）⇒ 三成（だるい）——
    只看 arousal 不行：平靜一小時 arousal 就衰減到 0，那是「平常」不是「累」；
  · 其他 ⇒ 照常。

擲骰按「角色 × 10 分鐘」穩定散列：同一段時間內重問不會一下做一下不做，重啟也一樣。
選了不做 ⇒ 生活迴圈照常休息（`BLUEAGENT_LIFE_IDLE_S`）之後再問一次；心情回來了就自然繼續。
"""

from __future__ import annotations

import hashlib

REST_CHANCE = {"しょんぼり": 0.5, "いらいら": 0.3}
#: 沒精神的門檻與機率
LOW_AROUSAL = 0.08
LOW_VALENCE = -0.1
LOW_ENERGY_CHANCE = 0.3
#: 擲骰的時間格（秒）
SLOT_S = 600


def roll(entity: str, now: float) -> float:
    """0–1 的穩定亂數（角色 × 10 分鐘一格）。"""
    digest = hashlib.sha256(f"{entity}:{int(float(now) // SLOT_S)}".encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def rest_reason(mood: dict, *, entity: str, now: float) -> str:
    """這次要不要先不做：回理由（`low_mood`／`irritated`／`low_energy`），照常做回空字串。"""
    if not mood:
        return ""
    label = str(mood.get("label") or "")
    chance, reason = REST_CHANCE.get(label, 0.0), {"しょんぼり": "low_mood", "いらいら": "irritated"}.get(label, "")
    if not chance and float(mood.get("arousal") or 0.0) < LOW_AROUSAL \
            and float(mood.get("valence") or 0.0) <= LOW_VALENCE:
        chance, reason = LOW_ENERGY_CHANCE, "low_energy"
    return reason if chance and roll(entity, now) < chance else ""


#: 給私人日誌的一句（不寫數字）
WORDS = {"low_mood": "なんだか気持ちが沈んでいて、いまは何も手につかない",
         "irritated": "少しいらいらしていて、いまは手を動かす気になれない",
         "low_energy": "なんだかだるくて、いまは何もしたくない"}


__all__ = ["REST_CHANCE", "WORDS", "rest_reason", "roll"]
