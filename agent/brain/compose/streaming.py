"""串流交付：一輪的幀按到達順序推出去，最後給一個 result；整輪結束寫進 outbox（斷線可補）。

review.md（2026-10-25）P1：從 `AppState.send_stream()` 拆出來；只接 `send` 與 outbox。
"""

from __future__ import annotations

import base64
import hashlib
import queue
import threading
import time

from agent.brain.compose.turns import reply_bubbles, reply_text
from agent.brain.domain.client_end import is_call
from agent.brain.runtime.delivery.outbox import merge_wavs, new_turn_id


def _asked_for_display(asked: dict) -> str:
    """outbox 的 `asked` 是**給客戶端顯示**的 ⇒ 不能帶內部標籤。

    貼圖回合的 prompt 文字是 `[[stamp_in:意味]] …`（那是給模型看的標籤），
    但客戶端看到的是**貼圖圖片**（`asked_sticker` 欄位）⇒ `asked` 這裡存成
    一句人話（「（スタンプ：意味） …」），不要漏出標籤。
    """
    text = str((asked or {}).get("text") or "")
    if "[[stamp_in:" not in text:
        return text
    import re
    def _swap(match):
        return "（スタンプ：%s）" % match.group(1)
    return re.sub(r"\[\[stamp_in:([^\]]*)\]\]", _swap, text)


class StreamDelivery:
    def __init__(self, *, send, outbox=None) -> None:
        #: `send(**kwargs) -> result`：AppState.send（含快照那一版）
        self.send = send
        self.outbox = outbox

    def stream(self, **kwargs):
        """跑一次 send，但把帧按到达顺序推出来；最后再给一个 result。

        **斷線可補**：一開始先推 `accepted{turn_id}`，整輪結束後把「她說的話」與音檔
        寫進 `TurnOutbox`（`data/outbox/`）。App 半路斷線只要記住 `turn_id`，
        重連後用 `GET /api/turns?since=` 就能把漏掉的那幾輪補回來（契約 v2）。
        """
        events: "queue.Queue[tuple[str, object]]" = queue.Queue()
        request_id = str(kwargs.get("request_id") or "").strip()
        if request_id:
            # 重試要回到原本那一輪，不能再發一個新門牌。雜湊不把使用者的 request_id 寫進日誌。
            namespace = "\0".join((str(kwargs.get("principal") or ""),
                                   str(kwargs.get("source_override") or ""),
                                   str(bool(kwargs.get("group"))),
                                   str(bool(kwargs.get("ownerless"))), request_id))
            turn_id = "r" + hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:24]
        else:
            turn_id = new_turn_id()
        clips: list[bytes] = []
        counts = {"frames": 0, "speech": 0}
        stickers: list[dict] = []
        photos: list[dict] = []
        # 分句「發出即落檔」要的身份（owner 2026-11-04）：一句話一發出去就先寫，
        # 不等語音、不等整輪收尾；這樣半路斷線／逾時／重啟都留得下她已經說出口的話。
        part_meta = {"principal": str(kwargs.get("principal") or ""),
                     "entity": str(kwargs.get("entity") or ""),
                     "asked": _asked_for_display(kwargs),
                     "source": str(kwargs.get("source_override") or ""),
                     "at": round(time.time(), 3)}
        first_part = {"meta": True}

        def emit(row) -> None:
            counts["frames"] += 1
            if isinstance(row, dict) and row.get("kind") == "sticker":
                payload = row.get("payload") or {}
                stickers.append({key: payload.get(key) for key in ("id", "entity", "url")})
            if isinstance(row, dict) and row.get("kind") == "photo":
                payload = row.get("payload") or {}
                # 委任出圖：App 用 url 畫、Discord 用 id 取檔（兩端同一個幀，owner 2026-11-04）
                photos.append({key: payload.get(key) for key in ("id", "url", "name")})
            if isinstance(row, dict) and row.get("kind") == "speech":
                counts["speech"] += 1
                audio = (row.get("payload") or {}).get("audio")
                if audio:
                    try:
                        clips.append(base64.b64decode(audio))
                    except Exception:                  # noqa: BLE001
                        pass
            if isinstance(row, dict) and row.get("kind") == "part" and self.outbox is not None:
                note = getattr(self.outbox, "note_part", None)
                if callable(note):
                    text = str((row.get("payload") or {}).get("text") or "")
                    note(turn_id, text, meta=part_meta if first_part["meta"] else None)
                    if text.strip():
                        first_part["meta"] = False
            events.put(("frame", row))

        def worker() -> None:
            clean: dict = {}
            text = ""
            bubbles: list = []
            try:
                result = self.send(**kwargs, on_frame=emit)
                text = reply_text(result)                 # 先取字（下面會把 frames 清掉）
                bubbles = reply_bubbles(result)           # 分段也要在清 frames 之前取（2026-11-04）
                clean = dict(result)
                clean["frames"] = []
                clean["turn_id"] = turn_id
                if isinstance(clean.get("decision"), dict):
                    clean["decision"] = {**clean["decision"], "frames": []}
                events.put(("result", clean))
            except Exception as exc:                   # noqa: BLE001 - 让客户端看到错误
                error = f"{type(exc).__name__}: {exc}"
                clean = {"ok": False, "finished": True, "error": error}
                events.put(("error", {"error": error, "turn_id": turn_id}))
            finally:
                self.save(turn_id, clean, clips, counts, kwargs, reply=text, bubbles=bubbles,
                          stickers=stickers, photos=photos)
                events.put(("end", None))

        events.put(("frame", {"kind": "accepted", "payload": {"turn_id": turn_id}}))
        threading.Thread(target=worker, daemon=True, name="ba-stream").start()
        while True:
            kind, payload = events.get()
            if kind == "end":
                return
            yield kind, payload

    def save(self, turn_id: str, result: dict, clips: list[bytes], counts: dict,
             kwargs: dict | None = None, *, reply: str = "", bubbles: list | None = None,
             stickers: list | None = None, photos: list | None = None) -> None:
        """把這一輪的交付寫進 outbox（失敗不影響任何事）。

        `bubbles`＝回覆分段（`reply_split` 的結果，跟 SSE `part` 幀同一份）⇒ 落成 `parts`，
        補拿／歷史才畫得出跟串流時一樣的多條氣泡（owner 2026-11-04）。
        """
        if self.outbox is None or result.get("duplicate"):
            # 重試沒有新交付；尤其不能收走原回合仍在寫的分句，或追加一筆空成功列。
            return
        # 分句在**發出當下**就已經落檔（不等語音、不看有沒有聽完）；這裡收成正式那一列。
        # ⚠️ `take_parts` 要防呆：測試／別的 outbox 實作（Mock、舊版）沒有這支 ⇒ 當沒有分句檔。
        take = getattr(self.outbox, "take_parts", None)
        try:
            meta, file_parts = take(turn_id) if callable(take) else ({}, [])
        except Exception:                        # noqa: BLE001 - 收分句檔失敗不影響交付
            meta, file_parts = {}, []
        meta = meta if isinstance(meta, dict) else {}
        file_parts = list(file_parts) if isinstance(file_parts, (list, tuple)) else []
        finished = result.get("finished") is not False
        if not finished and not file_parts and not stickers and not photos:
            # 連一句都沒發出去、也沒有圖的空／半截結果：不是可補拿的交付，也不能固化成終態。
            # ⚠️ 但**發過貼圖／圖**的一輪一定要留（owner 2026-11-04：她那輪只回了貼圖、
            #    文字是空的，整列被丟掉 ⇒ 歷史裡什麼都沒有）。
            return
        try:
            asked = kwargs or {}
            # 這一輪的來源端（`phone:owner`／`discord:owner`／`discord:vc`…）：run() 已經算出**生效的**來源
            # （`routing_for` 的結果，不是只有 source_override），落進 outbox 才能按端分流。
            source = str(result.get("source") or asked.get("source_override")
                         or meta.get("source") or "")
            # App 單人通話的內容**不進**對話紀錄（owner 2026-10-29）：只留掛斷時那一條標記
            # （`kind="call-log"`，在 `app/voice_api.py::hangup` 寫）⇒ 這裡標成 `"call"`，
            # `/api/turns` 會把 `kind="call"` 濾掉。
            row_kind = "call" if is_call(source) else ""
            parts = list(file_parts or bubbles or [])
            text = str(reply or result.get("text") or "") or "".join(parts)
            # 英文標記（owner 2026-11-04）：她發的貼圖用 `[[sticker:<id>]]` 記在正文裡，
            # 前台只認這一種寫法就畫得出來（不必再靠 `stickers` 陣列）。
            marks = [f"[[sticker:{str(item)}]]" for item in (result.get("sticker_ids") or []) if str(item)]
            if marks:
                text = (text + " " + " ".join(marks)).strip()
            error = str(result.get("error", "") or "")
            if not finished and not error:
                error = "incomplete"          # 逾時／中斷：句子留著，但這一輪不算成功
            decision = result.get("decision") or {}
            self.outbox.record(
                turn_id=turn_id,
                principal=str(asked.get("principal") or meta.get("principal") or ""),
                entity=str(asked.get("entity") or meta.get("entity") or ""),
                text=text,
                thread=str(decision.get("thread") or decision.get("thread_id") or ""),
                ok=bool(result.get("ok", not error)) and finished, error=error,
                asked=(str(asked.get("asked_text") or "") or _asked_for_display(asked)
                       or str(meta.get("asked") or "")),
                asked_parts=list(asked.get("asked_parts") or []) or None,
                source=source,
                audio=merge_wavs(clips), frames=counts.get("frames", 0),
                speech=counts.get("speech", 0),
                asked_sticker=str(asked.get("sticker") or ""), stickers=stickers,
                photos=photos, parts=parts, kind=row_kind)
        except Exception as exc:                        # noqa: BLE001
            print(f"[outbox] 寫入失敗（{type(exc).__name__}: {exc}）", flush=True)


__all__ = ["StreamDelivery"]
