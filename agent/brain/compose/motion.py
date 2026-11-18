"""每輪挑一個動作（`docs/PLAN-motion-kimodo-20261031.md` §5.2）：從回覆文字挑 tag，沒有合適的就**不發**。

為什麼是規則而不是讓主腦輸出 tag：規則沒有額外延遲、也不會把 prompt 弄髒；待機本來就會動，
所以寧缺勿濫。以後要更準，再讓主腦在回覆時順手給 tag（同一張表裡選）。
"""

from __future__ import annotations

#: (關鍵字, tag)；**順序即優先序**（先命中先算）。tag 只能是 §5.1 表裡那 12 個。
RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    # 「おはよ」不寫成「おはよう」：她口語常省掉「う」（2026-10-31 實測「おはよ、オーナー」沒中 greet、被後面的「うふふ」搶走）
    (("おはよ", "こんにちは", "こんばんは", "おかえり", "ただいま", "いらっしゃい", "やあ", "はじめまして"), "greet"),
    (("ごめん", "すみません", "申し訳", "わるい"), "bow"),
    (("うふふ", "あはは", "えへへ", "ふふっ", "あはっ"), "laugh"),
    (("はずかし", "てれ", "もうっ", "ばか"), "shy"),
    (("どうしよう", "かなあ", "たぶん", "考える", "うーん", "むむ"), "think"),
    (("やった", "すごい", "がんば", "最高", "うれし", "たのしみ"), "cheer"),
    (("えっ", "ええっ", "まさか", "びっくり", "うそ"), "surprised"),
    (("わからない", "知らない", "しょうがない", "しかたない"), "shrug"),
    (("さびし", "つらい", "かなし", "悲し", "さみし"), "sad"),
    (("ここ", "そこ", "これ", "あれ", "見て"), "point"),
    (("うん", "はい", "そうだね", "いいよ", "わかった", "了解", "たしかに"), "nod"),
    (("いや", "だめ", "ちがう", "違う", "ないよ", "むり"), "shake"),
)

#: 舞台只有 App 聊天那一輪（§5.2：通話與 Discord 不發）。
#: 空字串＝**App 直接打 `/api/turn` 而沒帶 `X-Source`**（2026-10-31 實測：舊包的 POST 沒送這個標頭，
#: 只送了 `X-Entity`／`X-Want-Voice`）；`api`＝圖／語音進來的同一條路。兩者都算 App。
#: `mr`＝MR 頭顯（2026-11-01）：她就站在你桌上，**通話中也有舞台**（手機通話沒有：畫面是通話頁）。
STAGE_SOURCES = ("phone", "api", "", "mr")
#: 明確不是舞台的來源：Discord、TRPG 桌、桌面端、心跳／主動訊息
BLOCKED_SOURCES = ("discord", "trpg", "desktop", "heartbeat", "proactive", "cron")


def tag_for(text: str) -> str | None:
    """回覆文字 → tag（沒有合適的 ⇒ None）。"""
    body = str(text or "")
    if not body:
        return None
    for words, tag in RULES:
        if any(word in body for word in words):
            return tag
    return None


class MotionPlanner:
    """把「這一輪的回覆」翻成一條 `motion` 幀（§5.4）。

    fail-open：庫是空的、片段壞了、`BLUEAGENT_MOTION=off` ⇒ 回 None，呼叫端照常把這輪講完。
    """

    def __init__(self, library=None, *, enabled: bool = True) -> None:
        self.library = library
        self.enabled = bool(enabled)

    def on_stage(self, source: str = "") -> bool:
        """這個來源有沒有舞台（App 聊天那一輪；Discord／TRPG／電話沒有）。"""
        if not self.enabled or self.library is None:
            return False
        head = str(source or "").split(":")[0].strip().lower()
        if head in BLOCKED_SOURCES or head not in STAGE_SOURCES:
            return False
        return head == "mr" or ":call" not in str(source or "").lower()

    def tags(self, source: str = "") -> list[str]:
        """這一輪可以讓模型挑的 tag（庫裡真的有片段的；沒有舞台 ⇒ 空）。"""
        if not self.on_stage(source):
            return []
        try:
            have = self.library.tags()
        except Exception:                               # noqa: BLE001 — 庫壞了就當沒有
            return []
        return [tag for _words, tag in RULES if have.get(tag)]

    def frame(self, *, entity: str, text: str, source: str = "", tag: str = "") -> dict | None:
        """`tag` 有給（模型自己在回覆裡標的）就用它；沒給才從回覆文字比規則。"""
        if not self.on_stage(source):
            return None
        tag = str(tag or "").strip().lower() or tag_for(text)
        if not tag:
            return None
        clip = self.library.pick(tag, entity=entity)     # 同一 tag 不連兩輪同一個 id
        if not clip:
            return None
        clip_id = str(clip.get("id") or "")
        frames, fps = int(clip.get("frames") or 0), int(clip.get("fps") or 0) or 30
        return {
            "id": clip_id,
            "tag": str(clip.get("tag") or tag),
            "url": f"/api/motion/{clip_id}",
            "hash": clip_id,
            "seconds": round(frames / float(fps), 3),
            "entity": str(entity or ""),
        }


__all__ = ["MotionPlanner", "RULES", "STAGE_SOURCES", "tag_for"]
