"""接收與打斷：音訊塊 → 斷句 → 一句話；主人一開口就打斷她；噪音不進管線。"""

from __future__ import annotations

import audioop
import threading
import time

from agent.adapter.discord_voice.audio import Utterance, _pcm16_to_wav
from agent.adapter.discord_voice.config import STT_RATE, VC_RATE


class VoiceReceive:
    """接收：收音訊塊、斷句、偵測開口（打斷）、濾噪音、把一句話交給管線。"""

    def _on_block(self, user_id: int, pcm48_stereo: bytes) -> None:
        """接收器交出的一塊（0.5 s 48k stereo）⇒ 切句。

        silero 要 16 kHz mono ⇒ 先轉；能量法那條路直接吃 48k stereo。
        """
        self.frames += 1
        if self.cutter is None:
            self.buffer.feed(str(user_id), pcm48_stereo, seconds=0.5)
            return
        import audioop
        mono = audioop.tomono(pcm48_stereo, 2, 0.5, 0.5)
        mono16, _ = audioop.ratecv(mono, 2, 1, VC_RATE, STT_RATE, None)
        # 同一個人連續講話 ⇒ silero 的環形緩衝只該有一條，所以按 user 分開切
        if self._active_speaker and self._active_speaker != str(user_id):
            self._flush_cutter()
        self._active_speaker = str(user_id)
        import numpy as np
        samples = np.frombuffer(mono16, dtype=np.int16)
        for clip in self.cutter.feed(samples):
            self.on_utterance(Utterance(user_id=str(user_id),
                                        wav=_pcm16_to_wav(clip.tobytes()),
                                        seconds=round(len(clip) / STT_RATE, 2)))

    def _flush_cutter(self) -> None:
        if self.cutter is None or not self._active_speaker:
            return
        for clip in self.cutter.flush():
            self.on_utterance(Utterance(user_id=self._active_speaker,
                                        wav=_pcm16_to_wav(clip.tobytes()),
                                        seconds=round(len(clip) / STT_RATE, 2)))
        self._active_speaker = ""

    # ── 打斷（barge-in）─────────────────────────────────────────────────────────
    def _on_speech_edge(self) -> None:
        """silero 偵測到「剛開口」（cutter 不知道是誰 ⇒ 用目前的說話者）。"""
        self.note_speech_start(self._active_speaker)

    def _on_speech_edge_user(self, user_id) -> None:
        """能量法那條路的上升沿（帶 user id）。"""
        self.note_speech_start(str(user_id))

    def note_speech_start(self, user_id) -> bool:
        """他開口了 ⇒ 如果她的聲音正在播，**立刻停**（回「有沒有真的打斷」）。

        邊界（交接檔 §6.1 第 5 點）：
          · 只有**同一個 VC 裡、白名單內的人**能打斷她；別人說話不觸發。
          · 她本來就沒在說話 ⇒ 什麼都不做（不是「一有人講話就 cancel」）。
          · `BLUEAGENT_VC_BARGE_IN=0` ⇒ 保留舊行為（聲音會重疊）。
        """
        user_id = str(user_id or "")
        if not self.config.barge_in:
            return False
        if not self.playing:
            return False
        if self.allowed_users and user_id not in self.allowed_users:
            return False
        self._epoch += 1
        self.tts_cancelled = True
        self.interruptions += 1
        self._stop_playback()
        self._log(f"vc: 打斷（user={user_id or '-'}，停掉她的聲音，第 {self.interruptions} 次）")
        if self.on_interrupt is not None:
            try:
                self.on_interrupt(user_id, self._epoch)
            except Exception as exc:                  # noqa: BLE001 - 通知失敗不影響停播
                self._log(f"vc: 打斷通知失敗 {type(exc).__name__}: {exc}")
        return True

    def _packet_watch(self) -> None:
        """每 5 秒報一次封包/區塊數（只在有變化時報，免得刷版）。"""
        last = -1
        while self.joined:
            threading.Event().wait(5.0)
            health = self.receiver.health() if self.receiver is not None else {}
            packets = int(health.get("packets") or 0)
            if packets != last:
                last = packets
                self._log(f"vc: RTP 封包 {packets} → 音訊塊 {self.frames}"
                          f"（dave={'on' if health.get('dave') else 'off'}）")

    def on_utterance(self, utterance: Utterance) -> None:
        """收到一句講完的話（在 voice_recv 的執行緒裡）⇒ 轉寫 → 走同一條管道。"""
        self.utterances += 1
        threading.Thread(target=self._process, args=(utterance,), daemon=True,
                         name=f"vc-{utterance.user_id}").start()

    @staticmethod
    def _is_noise(text: str) -> bool:
        """轉寫碎片是不是雜訊（「。」「The.」這種）。

        規則刻意單純：去掉標點與空白後①長度 ≤2 ②或（日語鎖定下）完全沒有假名／漢字
        ⇒ 當雜訊丢掉。**不猜內容**，只擋「看起來不是話」的東西。
        """
        core = "".join(ch for ch in str(text or "") if ch.isalnum())
        # 只有 0–1 個字（「。」「あ」）才當雜訊：**不能按長度一刀切**，
        # 「行く」「しま」這種兩字日語是真話（它們會被 merge 併成整句）。
        if len(core) <= 1:
            return True
        has_jp = any("\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff" for ch in core)
        return not has_jp

    def _process(self, utterance: Utterance) -> None:
        stt = getattr(self.source.state.app, "stt", None)
        if stt is None:
            self._log("vc: 沒有 STT，忽略這一句")
            return
        # 順便判「說完了沒」（和糾錯並行）：沒講完的碎片多等一下再送（見 `_buffer_fragment`）
        extra = {"judge": True} if getattr(stt, "end_judge", None) is not None else {}
        transcript = stt.transcribe(utterance.wav, language="ja", **extra)
        if not transcript.ok:
            self._log(f"vc: 轉寫失敗（{transcript.error or 'empty'}）")
            return
        text = str(transcript.text or "").strip()
        self._log(f"vc: 轉寫『{text[:40]}』（{utterance.seconds}s, {transcript.probability:.2f}）")
        if self._is_noise(text):
            self.noise_dropped += 1
            self._log(f"vc: 丢掉雜訊碎片『{text[:20]}』（累計 {self.noise_dropped}）")
            return
        self._buffer_fragment(str(utterance.user_id), text,
                              complete=getattr(transcript, "complete", None))

    def _gap_for(self, user_id: str) -> float:
        """這個人的碎片要等多久才送：最後一片判成「沒講完」⇒ 再多等 `hold_extra_s`。"""
        incomplete = getattr(self, "_pending_incomplete", {}).get(user_id)
        return self.merge_gap_s + (getattr(self, "hold_extra_s", 0.0) if incomplete else 0.0)

    def _buffer_fragment(self, user_id: str, text: str, *, complete: bool | None = None) -> None:
        """把碎片暫存；`merge_gap_s` 內沒有新碎片就併成一句送出去。

        2026-10-30：最後一片被判成「沒講完」（`complete=False`）⇒ 等待再加 `hold_extra_s`
        （`BLUEAGENT_VC_HOLD_S`）。判不出來（None）照舊。
        """
        with self._pending_lock:
            rows = self._pending.setdefault(user_id, [])
            rows.append(text)
            self._pending_at[user_id] = time.time()
            if not hasattr(self, "_pending_incomplete"):
                self._pending_incomplete = {}
            self._pending_incomplete[user_id] = complete is False
            joined = "".join(rows)
            if self.merge_gap_s <= 0 or len(joined) >= 160:
                pending = rows[:]
                self._pending[user_id] = []
            else:
                pending = None
        if pending is not None:
            self._respond(user_id, "".join(pending))
            return

        def waiter() -> None:
            while True:
                time.sleep(max(0.05, self.merge_gap_s / 2))
                with self._pending_lock:
                    last = self._pending_at.get(user_id) or 0.0
                    if (time.time() - last) < self._gap_for(user_id):
                        continue
                    rows = self._pending.get(user_id) or []
                    self._pending[user_id] = []
                    self._pending_at.pop(user_id, None)
                    self._pending_incomplete.pop(user_id, None)
                if len(rows) > 1:
                    self.merged += 1
                    self._log(f"vc: 併了 {len(rows)} 片碎片 →『{''.join(rows)[:40]}』")
                if rows:
                    self._respond(user_id, "".join(rows))
                return

        threading.Thread(target=waiter, daemon=True, name=f"vc-merge-{user_id}").start()

    def _respond(self, user_id: str, text: str) -> None:
        """一句（合併後的）話 ⇒ 走同一條管道回她。"""
        # 在語音頻道裡說話＝對她說（同一個房間），但仍然走注意力層（會是 REPLY）
        epoch = self._epoch                     # 打斷世代：這一輪的聲音屬於這個世代
        # 帶 channel_id ⇒ 「先短回一句、完整答案後面補」那條路才知道要把後續貼回同一個頻道
        result = self.source.handle_voice(text, user_id,
                                          channel_id=self.config.channel_id)
        if not result.get("ok"):
            self._log(f"vc: 這句不回（{result.get('reason')}）")
            return
        # 頻道裡也貼一份文字：她在語音裡說話時，文字聊天裡看不到任何東西會讓人以為她沒收到
        text = str(result.get("text") or "").strip()
        if text:
            self._post_text(text)
        self._publish(result, epoch=epoch)
