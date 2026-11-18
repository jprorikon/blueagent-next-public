"""播放與發佈：PCM 播放佇列、打斷時停播、音檔發佈與文字貼文、`say`。"""

from __future__ import annotations

import io
import threading


class VoicePlayback:
    """播放：排隊播 PCM、被打斷就停、發佈音檔、貼文字、說一句。"""

    def _stop_playback(self) -> bool:
        """停掉她正在播的聲音（`VoiceClient.stop()`；之後那支 `after` 會把 `playing` 關掉）。"""
        client = self.voice_client
        with self._play_lock:
            # 排隊中還沒播的也一起丢掉：打斷是「她現在閉嘴」，不是「換下一段」
            self._pcm_queue.clear()
            self.playing = False
        if client is None:
            return False
        try:
            client.stop()
            return True
        except Exception as exc:                      # noqa: BLE001
            # 已經停了／還沒播過 ⇒ discord.py 會抛，不算失敗
            self._log(f"vc: 停止播放失敗（{type(exc).__name__}: {exc}）")
            return False

    def _on_play_done(self, error=None) -> None:
        """一段聲音播完（或被 stop 掉）：清狀態，然後接著播下一段（如果有的話）。"""
        if error is not None:
            self._log(f"vc: 播放結束但有錯（{error}）")
        with self._play_lock:
            self.playing = False
            self._drain_locked()

    def _queue_pcm(self, pcm: bytes) -> None:
        """把一段 PCM 排進播放佇列（依序播，不互相打斷、不被 `Already playing audio` 丢掉）。"""
        with self._play_lock:
            self._pcm_queue.append(pcm)
            self._drain_locked()

    def _drain_locked(self) -> None:
        """手上沒在播、佇列裡有東西 ⇒ 開始播下一段。呼叫端必須持 `_play_lock`。"""
        if self.playing or not self._pcm_queue:
            return
        client = self.voice_client
        if client is None or not client.is_connected():
            self._pcm_queue.clear()
            return
        pcm = self._pcm_queue.popleft()
        try:
            import discord
            self.playing = True
            self.tts_cancelled = False
            # ⚠️ `PCMAudio` 是一次性 buffer：每段聲音都要新建一個
            client.play(discord.PCMAudio(io.BytesIO(pcm)), after=self._on_play_done)
        except Exception as exc:                      # noqa: BLE001
            self.playing = False
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._log(f"vc: 播放失敗 {self.last_error}")

    # ── 出聲 ────────────────────────────────────────────────────────────────
    def _post_text(self, text: str) -> bool:
        sender = getattr(self.source, "send_channel_text", None)
        if sender is None:
            return False
        try:
            posted = bool(sender(self.config.channel_id, text))
        except Exception as exc:                          # noqa: BLE001
            self._log(f"vc: 文字貼回頻道失敗（{type(exc).__name__}: {exc}）")
            return False
        # 一行觀測：貼成功也記，否則「看不到字」只能猜是沒貼還是沒貼上去
        if not posted:
            self._log("vc: 文字沒貼成（Discord client／loop 還沒就緒）")
        return posted

    def publish_audio(self, audio: bytes) -> bool:
        """把一段現成的 wav 播進語音房（給「先短回一句、後面補完整答案」那條路用）。

        一樣受打斷世代保護：已經被打斷的那一輪，後面這段不播。
        """
        if not audio:
            return False
        self._publish({"audio": audio}, epoch=self._epoch)
        return True

    def _publish(self, result: dict, *, epoch: int | None = None) -> None:
        if not self.config.speak_in_voice or not result.get("audio"):
            return
        client = self.voice_client
        if client is None or not client.is_connected():
            return
        if epoch is not None and epoch != self._epoch:
            # 這段聲音所屬的那一輪已經被主人打斷 ⇒ 不要再播（半句話不該事後補上）
            self._log("vc: 打斷後丢棄已作廢的那段聲音")
            return
        path = None
        try:
            # 現役的做法：wav → 48k stereo s16le PCM → PCMAudio（不用 ffmpeg 外部檔）
            from agent.adapter.discord_vc_receiver import BLOCK_BYTES  # noqa: F401
            pcm = self._wav_to_pcm(result["audio"])
            # 排隊播：上一段（例如短回覆）還在播時，不能直接 `play()`（會被丢）
            self._queue_pcm(pcm)
            self._log(f"vc: 回了 {len(result['audio'])} bytes 的聲音")
        except Exception as exc:                      # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.playing = False
            self._log(f"vc: 播放失敗 {self.last_error}")
        finally:
            if path is not None:
                threading.Timer(30.0, lambda p=path: p.unlink(missing_ok=True)).start()

    @staticmethod
    def _wav_to_pcm(wav: bytes) -> bytes:
        """wav（她的是 32 kHz mono）→ Discord 播放要的 48 kHz stereo s16le。"""
        import subprocess
        process = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le",
             "-ar", "48000", "-ac", "2", "pipe:1"],
            input=wav, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if process.returncode != 0:
            raise RuntimeError(process.stderr[:160].decode("utf-8", "replace"))
        return process.stdout

    def say(self, text: str) -> bool:
        """把一句話用她的聲音說進頻道（不經過一輪對話）。"""
        body = str(text or "").strip()
        tts = getattr(self.source, "tts", None) if self.source is not None else None
        tts = tts or getattr(getattr(self.source, "state", None), "app", None)
        engine = getattr(tts, "tts", None) if tts is not None else None
        try:
            if engine is None:
                return False
            clips = list(engine.speak(body, voice=getattr(self, "voice_name", "") or ""))
            audio = b"".join(bytes(clip.get("audio") or b"") for clip in clips if isinstance(clip, dict))
            if not audio:
                return False
            self._publish({"audio": audio, "voice": ""}, epoch=self._epoch)
            return True
        except Exception as exc:                          # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._log(f"vc: 主動發話失敗 {self.last_error}")
            return False
