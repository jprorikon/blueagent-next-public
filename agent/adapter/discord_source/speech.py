"""語音與音檔：VC 的輸入、speech 幀的播放、WAV 合併／解碼、語音頻道指令。"""

from __future__ import annotations

import io
import os


class DiscordSpeech:
    """語音：VC 進來的話、逐句播放、後續答案出聲、音檔合併、VC 指令。"""

    def _vc_stream_play(self) -> bool:
        """語音房逐句播放的總開關（`BLUEAGENT_VC_STREAM_PLAY`，預設開）。"""
        return bool(self.voice is not None and str(
            os.environ.get("BLUEAGENT_VC_STREAM_PLAY") or "1").strip().lower()
            not in {"0", "off", "false", "no"})

    def publish_speech_frame(self, frame) -> bool:
        """把單一 `speech` 幀（SSE 內層）的 wav 立刻排進語音房播放佇列。

        逐句串流的三條路（短回覆／一般回覆／後續完整答案）都走這一個入口，
        差別只在「誰在什麼時候呼叫它」。
        """
        if self.voice is None or not isinstance(frame, dict):
            return False
        body = frame.get("payload") if isinstance(frame.get("payload"), dict) else frame
        if str(frame.get("kind") or "") not in {"speech", ""}:
            return False
        audio = (body or {}).get("audio")
        if not audio:
            return False
        try:
            return bool(self.voice.publish_audio(self._decode_audio(audio)))
        except Exception as exc:                          # noqa: BLE001
            self._log(f"vc: 逐句播放失敗（{type(exc).__name__}: {exc}）")
            return False

    def speak_followup(self, result: dict) -> bytes:
        """把「後續完整答案」的聲音唸進語音房（回那段 wav；沒有就空 bytes）。

        為什麼要單獨一支：`send_voice()` 的後續工作是**直接 `send()`**，
        它的 `speech` 幀不會被任何人播放——不主動接這一下，完整答案就只有文字。
        """
        audio = self.audio_from_frames(result.get("frames") or ())
        if not audio:
            return b""
        voice = self.voice
        if voice is None:
            return audio
        try:
            voice.publish_audio(audio)
        except Exception as exc:                          # noqa: BLE001
            self._log(f"discord: 後續答案播放失敗（{type(exc).__name__}: {exc}）")
        return audio

    def handle_voice(self, text: str, author_id: str, *, channel_id: str = "") -> dict:
        """語音頻道裡說的話：在同一個房間 ⇒ 視為對她說（仍然經過注意力層）。"""
        return self.handle(text, author_id, guild=True, mentioned=False, is_dm=False,
                           force_addressed=True, channel_id=channel_id)

    @classmethod
    def audio_from_frames(cls, frames) -> bytes:
        """從一輪的 frames 裡撿出 `speech` 幀的 wav 併成一個檔（語音頻道播出用）。

        為什麼需要這個：語音源的短回覆走的是「直接 `send()`」那條路，`handle()` 下面的
        frame 迴圈（給文字源用的）沒有跑到它，所以音訊不會自己出現在 `audio` 欄位裡。
        漏掉這一步的症狀＝**她明明合成了（turns.jsonl 有 `speech`)但頻道裡沒有聲音**。
        """
        clips: list[bytes] = []
        for frame in frames or ():
            if not isinstance(frame, dict):
                continue
            if str(frame.get("kind") or "") != "speech":
                continue
            body = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
            audio = body.get("audio")
            if not audio:
                continue
            try:
                clips.append(cls._decode_audio(audio))
            except Exception:                      # noqa: BLE001 - 壞一段不影響其他句
                continue
        return cls.merge_wavs(clips)

    @staticmethod
    def merge_wavs(clips: list[bytes]) -> bytes:
        """把分句合成的一串 wav 併成一個檔（Discord 一次只發一個附件）。"""
        import io as _io
        import wave as _wave
        good = [clip for clip in clips if clip and clip[:4] == b"RIFF"]
        if not good:
            return b""
        if len(good) == 1:
            return good[0]
        out = _io.BytesIO()
        # ⚠️ `wave.open` 吃檔名或 file-like，**不吃 bytes** —— 一定要包 BytesIO
        with _wave.open(_io.BytesIO(good[0]), "rb") as first:
            params = first.getparams()
        with _wave.open(out, "wb") as target:
            target.setparams(params)
            for clip in good:
                with _wave.open(_io.BytesIO(clip), "rb") as source:
                    target.writeframes(source.readframes(source.getnframes()))
        return out.getvalue()

    @staticmethod
    def _decode_audio(audio_b64: str) -> bytes:
        """speech 幀裡的 base64 wav → 位元組（Discord 直接當附件發）。"""
        import base64
        try:
            return base64.b64decode(str(audio_b64 or ""))
        except Exception:                                # noqa: BLE001
            return b""

    def _voice_command(self, command: str, *, entity: str = "") -> dict:
        """「进来／退吧」這種一句話命令 ⇒ 真的進房／退房，然後回一句短話。"""
        voice = self.voice
        if command == "join":
            ok = voice.request_join("owner_asked")
            text = "好，我进去。" if ok else "进不去（Discord 那边没连上？）"
        else:
            ok = voice.request_leave("owner_asked")
            text = "好，我先退了。" if ok else "我本来就不在语音房里。"
        self._log(f"discord: 語音房命令 {command}（{'ok' if ok else 'failed'}）")
        return {"ok": True, "principal": "owner", "entity": entity or "",
                "text": text, "audio": b"", "frames": 0,
                "voice_command": command, "voice_command_ok": bool(ok)}
