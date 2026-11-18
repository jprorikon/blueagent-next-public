"""語音回合「先出聲、再幹活」：短回覆立刻說，真正的工作排進主人指令佇列，做完再交付。

review.md（2026-10-25）P1：從 `AppState.send_voice()` 拆出來；只接 `send`（跑一輪）、
指令佇列與推送匯流排，不接整個 App。
"""

from __future__ import annotations

import os
import threading

from agent.brain.compose.turns import reply_text


def same_gist(left: str, right: str, *, threshold: float = 0.72) -> bool:
    """兩句話是不是「同一個意思」（去標點後比相似度）。

    只用來擋「短回覆與後續答案幾乎一字不差」的情況，不是語意判斷：
    真的補了新資訊（長度差很多、內容不同）就會是 False。
    """
    import difflib

    def norm(text: str) -> str:
        return "".join(ch for ch in str(text or "") if ch.isalnum()).lower()

    one, two = norm(left), norm(right)
    if not one or not two:
        return False
    if one == two:
        return True
    shorter, longer = sorted((one, two), key=len)
    if len(shorter) >= 8 and shorter in longer:
        return True
    return difflib.SequenceMatcher(None, one, two).ratio() >= threshold


class VoiceTurns:
    def __init__(self, *, send, commands, bus=None) -> None:
        #: `send(text, **kwargs) -> result`：AppState.send（含快照那一版）
        self.send = send
        self.commands = commands
        self.bus = bus

    def send_voice(self, text: str, *, entity: str = "mira", principal: str = "owner",
                   mode: str = "work", source: str = "discord:vc",
                   on_followup=None, **kwargs) -> dict:
        """語音來源的回合。

        規矩（owner 2026-10-22）：
          · 語音來源本身優先級最高（`SourceClass.REALTIME`，並行、可搶佔）。
          · **但「要幹活」的語音回合不能讓她沉默幾十秒**：先回一句短的（沒有工具、沒有委任），
            立刻出聲；真正的工作再以**普通優先級**（INTERACTIVE）排進佇列，做完再用
            `on_followup` 送出去（Discord：第二條訊息 + 語音）。
        """
        # HTTP 那條（`/api/audio` + `X-Mode: work`）把來源放在 `source_override`（`api:audio`）：
        # 以前在這裡被丟掉、退回預設的 `discord:vc` ⇒ App 的語音回合與它的後續交付都被標成 Discord
        # （2026-10-28 複檢：後續交付落 outbox 時照來源分端，標錯就從 App 的歷史裡消失）。
        override = str(kwargs.pop("source_override", "") or "").strip()
        if override:
            source = override
        # ⚠ 原本寫成「pop 一次判 None、再一次 bool(pop())」⇒ 明確傳 `ack_first=True` 時
        # 第二次 pop 直接 KeyError（2026-10-22 測試抓到）。先取出來再判。
        raw_ack_first = kwargs.pop("ack_first", None)
        ack_first = (str(os.environ.get("BLUEAGENT_ACK_FIRST") or "1").strip().lower()
                     not in {"0", "off", "false", "no"}) if raw_ack_first is None \
            else bool(raw_ack_first)
        heavy = str(mode or "chat") == "work"
        if not heavy or not ack_first:
            return self.send(text, entity=entity, principal=principal, mode=mode,
                             source_override=source, **kwargs)
        # 語音源要出聲 ⇒ **完整答案也要出聲**（2026-10-22 實測：只有短回覆有聲音，
        # 聽起來像「有一段話沒被讀出來」）。逐句串流的掛鉤只給後續完整答案用：
        # 短回覆那輪由 handle() 自己播，兩邊都掛會同一句話播兩次。
        frame_hook = kwargs.pop("on_speech_frame", None)
        want_voice = bool(kwargs.get("want_voice"))
        kwargs.pop("want_voice", None)          # 短回覆一定要出聲，呼叫端的值不重複傳
        kwargs.pop("mode", None)                # mode 由這裡決定（短回覆固定 chat）
        ack = self.send(text, entity=entity, principal=principal, mode="chat",
                        quick=True, want_voice=True, source_override=source, **kwargs)
        kwargs.pop("input_timing", None)        # 語音那段耗時記在短回覆那輪就好，後續不重記
        if want_voice:
            kwargs["want_voice"] = True         # 後續完整答案也合成（交付端決定播不播）
        # 已經說過的那句交出去：完整答案的 prompt 會要求「只補新內容」（owner 2026-10-22）
        ack_text = reply_text(ack)
        kwargs["ack_text"] = ack_text
        on_followup = self._dedupe(on_followup, ack_text)
        if frame_hook is not None:
            kwargs["on_speech_frame"] = frame_hook
        submitted = self._submit(text, entity=entity, principal=principal, source=source,
                                 on_followup=on_followup, kwargs=kwargs)
        ack = dict(ack)
        ack["following_up"] = True
        if submitted.get("ok"):
            ack["command_id"] = submitted.get("id")
        return ack

    def _dedupe(self, on_followup, ack_text: str):
        """「後續答案跟短回覆幾乎一樣」⇒ 不送（owner 2026-10-22：「感觉对话是混乱的」——
        語音房裡先問一句「どの子にする？」、兩秒後又把同一句問一次，聽起來像鬼打牆）。"""
        dedupe_on = str(os.environ.get("BLUEAGENT_FOLLOWUP_DEDUPE") or "1").strip().lower() \
            not in {"0", "off", "false", "no"}
        if on_followup is None or not ack_text or not dedupe_on:
            return on_followup

        def dedup(follow, _original=on_followup, _ack=ack_text):
            body = reply_text(follow)
            if same_gist(body, _ack):
                print(f"[followup] 與短回覆幾乎相同 ⇒ 丟掉（『{str(body)[:24]}』）", flush=True)
                return
            _original(follow)

        return dedup

    def _submit(self, text: str, *, entity: str, principal: str, source: str,
                on_followup, kwargs: dict) -> dict:
        """後續工作排進**主人指令佇列**：同一個角色一次只跑一條（連交代三件事不會三條搶腦／搶嘴），
        `GET /api/commands` 看得到排到第幾條。佇列滿了退回「開一條執行緒」，別把話丢掉。"""
        extra = dict(kwargs)
        extra["source"] = source

        def deliver(payload: dict) -> None:
            result = (payload or {}).get("result") or {}
            self.publish(result, entity=entity, principal=principal, source=source)
            if on_followup is not None:
                try:
                    on_followup(result)
                except Exception as exc:                  # noqa: BLE001
                    print(f"[followup] 交付失敗（{type(exc).__name__}: {exc}）", flush=True)

        submitted = self.commands.submit(text, entity=entity, principal=principal,
                                         source=source, meta=extra, on_done=deliver)
        if not submitted.get("ok"):
            print(f"[followup] 佇列拒收（{submitted.get('reason')}）⇒ 直接跑", flush=True)
            threading.Thread(target=self.followup_worker,
                             args=(text, entity, principal, source, on_followup, dict(kwargs)),
                             name=f"ba-followup-{entity}", daemon=True).start()
        return submitted

    def publish(self, result: dict, *, entity: str, principal: str, source: str) -> None:
        """後續交付推到 App 的常駐 SSE（沒有請求可以回，只能推）。"""
        if self.bus is None:
            return
        self.bus.publish(str(principal), {
            "kind": "delivery", "text": reply_text(result),
            "entity": entity, "principal": principal, "source": source,
            "following_up": False,
            "frames": len(result.get("frames") or []),
            "thread": str((result.get("decision") or {}).get("thread") or ""),
        })

    def followup_worker(self, text: str, entity: str, principal: str, source: str,
                        on_followup, extra: dict) -> None:
        try:
            extra.pop("quick", None)
            result = self.send(text, entity=entity, principal=principal, mode="work",
                               followup=True, source_override=source, **extra)
        except Exception as exc:                          # noqa: BLE001 - 背景任務不該炸服務
            print(f"[followup] 失敗（{type(exc).__name__}: {exc}）", flush=True)
            return
        # App 側沒有「請求可以回」——補的那條走推送（`GET /api/events` 長連）
        self.publish(result, entity=entity, principal=principal, source=source)
        if on_followup is not None:
            try:
                on_followup(result)
            except Exception as exc:                      # noqa: BLE001
                print(f"[followup] 交付失敗（{type(exc).__name__}: {exc}）", flush=True)


__all__ = ["VoiceTurns", "same_gist"]
