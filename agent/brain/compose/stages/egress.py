"""出口：語音、產物、最後的回覆幀。"""

from __future__ import annotations

from agent.brain.compose.stages.context import TurnContext
from agent.brain.runtime.voice import VoiceService

from agent.brain.compose.stages.speech_text import _remaining_after_spoken


class VoiceEgressStage:
    name = "voice"

    def __init__(self, voice: VoiceService, motion=None) -> None:
        self.voice = voice
        #: 動作規劃器（`app/motion.py`）；None＝這一版不發 `motion` 幀
        self.motion = motion

    def run(self, env, ctx):
        output = TurnContext.of(env).output
        reply = str(output.reply_text or "")
        if not reply:
            return None
        # 動作平常由 brain 在她**開口時**發（`brain._Motion`）；這裡只補「沒走 brain 串流」的回覆。
        # 只看 App 聊天那一輪；庫裡沒有合適的 tag ⇒ 不發，聊天照常。
        # ⚠️ 必須放在「串流時已唸完 ⇒ 直接 return」**之前**：首句優先那條路會把整段在串流時唸掉，
        #    走到下面 `return None` 就再也回不來了 —— 2026-10-31 實機就是這樣「嘴動、身體不動」。
        if self.motion is not None and not output.motion_sent:
            try:
                frame = self.motion.frame(entity=env.decision.entity, text=reply,
                                          source=str(getattr(env.event, "source", "") or ""))
            except Exception as exc:                     # noqa: BLE001 — 動作壞了不能拖垮這一輪
                print(f"motion: 挑動作失敗（{exc!r}）⇒ 這輪沒有動作", flush=True)
                frame = None
            if frame:
                ctx.emit("motion", dict(frame))
                print(f"motion: {frame.get('tag')} → {frame.get('id')}（{frame.get('seconds')}s）", flush=True)
        # 首句優先那條路已經把前面幾句唸掉了（`spoken_text`）⇒ 這裡只補剩下的尾巴。
        # ⚠ 用「去掉空白後」的比較：回覆常含空行，字面 startswith 會失敗 ⇒ 整段被唸第二遍
        # （owner 2026-10-22 回報「附帶語音重複兩遍文字內容」，ASR 驗到整段出現兩次）。
        spoken = str(output.spoken_text or "")
        if spoken:
            rest = _remaining_after_spoken(reply, spoken)
            if rest == "":
                # 串流時已整段唸完 ⇒ 這裡**絕對不能**再合成（否則音檔就是文字唸兩遍）
                print(f"voice: 整段已在串流時唸過（{len(spoken)} 字）⇒ 不重合成", flush=True)
                return None
            if rest == reply.strip() and len(spoken) > 8:
                # 對不上（回覆被改寫／排版不同）⇒ 只能整段再唸一次，但留下證據
                print(f"voice: 唸過的段落與回覆接不上（spoken {len(spoken)} 字 / "
                      f"reply {len(reply)} 字）⇒ 整段重合成", flush=True)
            reply = rest
        target = str(output.voice_target or "")
        frames = self.voice.maybe_speak(
            env.decision.entity, reply, thread_id=env.thread.id,
            principal=env.decision.principal,
            force=bool(output.force_voice),
            target="" if target == "auto" else target,
            source=env.event.source,
        )
        try:
            for frame in frames:
                ctx.emit(frame.get("kind", "speech"), dict(frame.get("payload") or {}))
        finally:
            # 只有「本次 force 语音」拿到的 floor 才释放；通话的 floor 由 hangup 释放。
            self.voice.release_floor(env.decision.entity, env.thread.id)
        return None


class ArtifactStage:
    name = "artifact"

    def run(self, env, ctx):
        return None


class EgressStage:
    name = "egress.app"

    def run(self, env, ctx):
        return None
