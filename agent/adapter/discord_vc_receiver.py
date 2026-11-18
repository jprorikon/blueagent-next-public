"""Discord 語音接收（**照搬現役 discordvc 那套已跑通的**，owner 2026-10-22 指正）。

為什麼不用 `discord-ext-voice-recv`：Discord 現在的語音走 **DAVE（E2EE）**，
那個擴充不處理 DAVE ⇒ socket 明明有封包，解出來是密文，表現就是「一塊音訊都收不到」。
現役 `blueagent/discordvc/discord_voice_bot.py` 的 `VoiceReceiver` 早就把這條路走通了：

    RTP 解包 → NaCl(aead_xchacha20_poly1305_rtpsize) 解密 → DAVE 解密（davey）
    → 每個 SSRC 一個 opus 解碼器 → 48 kHz stereo PCM

這裡只搬「收包」這一段，其餘（切句、STT、回話）在新倉自己的層做。
兩個從現役學到的硬教訓照抄，不重新踩：

  1. **DAVE 是按 user_id 解的**：SSRC→user 映射拿不到時（重進頻道後 Discord 常常不補
     SPEAKING 事件）⇒ 拿不到 user_id 就不能解密，餵給 opus 的就是密文（聽起來有能量，
     其實是噪聲）。退路＝「頻道裡只有一個非 bot 成員」才敢認定。
  2. **收包執行緒上不准做重活**：現役實測在 socket reader 裡跑 ffmpeg/HTTP，
     每塊堵 100–200 ms ⇒ 掉音。這裡只入列，worker 去做。
"""

from __future__ import annotations

import queue
import struct
import threading
import time
from collections import defaultdict

#: 一塊 0.5 s @48k stereo s16le。
BLOCK_BYTES = 48000 * 2 * 2 // 2


def tail_block_due(last_pkt: float, now: float, tail_s: float, sent: int,
                   tail_max: int, last_tail: float, *, gap_s: float = 0.5) -> bool:
    """停嘴之後該不該補一塊靜音（見 `VoiceReceiver._silence_pump`）。"""
    if not last_pkt or tail_s <= 0 or tail_max <= 0 or sent >= tail_max:
        return False
    if (now - last_pkt) < tail_s:
        return False
    return (now - last_tail) >= gap_s


def load_opus() -> bool:
    """把系統 libopus 載進 discord.py（不然 `discord.opus.Decoder()` 會炸）。"""
    import discord
    if discord.opus.is_loaded():
        return True
    for name in ("libopus.so.0", "libopus.so", "opus"):
        try:
            discord.opus.load_opus(name)
            if discord.opus.is_loaded():
                return True
        except Exception:                              # noqa: BLE001
            continue
    return discord.opus.is_loaded()


class VoiceReceiver:
    """把 Discord 的 RTP 變成「某人 + 0.5 秒 PCM 塊」的 callback。"""

    SILENCE_BLOCK = bytes(BLOCK_BYTES)

    def __init__(self, vc, on_block, *, allowed_users=(), log=None,
                 tail_s: float = 0.0, tail_max: int = 6) -> None:
        self.vc = vc
        self.on_block = on_block
        self.allowed = {str(item) for item in (allowed_users or ()) if str(item)}
        self.log = log or (lambda _message: None)
        self.tail_s = max(0.0, float(tail_s))
        self.tail_max = max(0, int(tail_max))

        conn = vc._connection
        self._secret_key = bytes(conn.secret_key)
        self._dave_session = getattr(conn, "dave_session", None)
        self._bot_ssrc = conn.ssrc

        self._ssrc_to_user: dict[int, int] = {}
        self._lock = threading.Lock()
        self._pend: dict[int, bytearray] = defaultdict(bytearray)
        self._decoders: dict[int, object] = {}
        self.packets = 0
        self.blocks = 0
        self._count = 0
        self._last_pkt = 0.0
        self._tail_sent = 0
        self._tail_user = 0
        self._last_tail = 0.0
        self._fallback_user = self._infer_single_allowed()
        self._q: "queue.Queue[tuple[int, bytes]]" = queue.Queue(maxsize=256)
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._work, daemon=True, name="vc-blocks")
        self._worker.start()
        self._install_speaking_hook(conn)
        conn.add_socket_listener(self._on_packet)
        if self.tail_s > 0 and self.tail_max > 0:
            threading.Thread(target=self._silence_pump, daemon=True,
                             name="vc-silence").start()
        self.log(f"receiver: 起（bot_ssrc={self._bot_ssrc} "
                 f"dave={'on' if self._dave_session else 'off'} "
                 f"fallback_user={self._fallback_user or '-'}）")

    # ── 內部 ────────────────────────────────────────────────────────────────
    def _work(self) -> None:
        while True:
            try:
                user_id, block = self._q.get(timeout=0.2)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            self.blocks += 1
            try:
                self.on_block(user_id, block)
            except Exception as exc:                   # noqa: BLE001
                self.log(f"receiver: 回呼失敗 {type(exc).__name__}: {exc}")

    def _silence_pump(self) -> None:
        while not self._stop.is_set():
            time.sleep(0.05)
            now = time.monotonic()
            if not tail_block_due(self._last_pkt, now, self.tail_s, self._tail_sent,
                                  self.tail_max, self._last_tail):
                continue
            self._last_tail = now
            self._tail_sent += 1
            try:
                self._q.put_nowait((self._tail_user, self.SILENCE_BLOCK))
            except queue.Full:
                return

    def _infer_single_allowed(self) -> int:
        """頻道裡恰好一個非 bot 成員、而且他在白名單裡 ⇒ 那個 SSRC 就是他。"""
        try:
            channel = self.vc.channel
            if not channel:
                return 0
            bot_id = self.vc.user.id if self.vc.user else 0
            members = [member.id for member in channel.members if member.id != bot_id]
            if len(members) != 1:
                return 0
            only = members[0]
            if self.allowed and str(only) not in self.allowed:
                return 0
            return only
        except Exception:                              # noqa: BLE001
            return 0

    def _install_speaking_hook(self, conn) -> None:
        original = conn.hook
        me = self

        async def hook(ws, msg):
            if isinstance(msg, dict) and msg.get("op") == 5:
                data = msg.get("d") or {}
                if data.get("ssrc") and data.get("user_id"):
                    with me._lock:
                        me._ssrc_to_user[int(data["ssrc"])] = int(data["user_id"])
                    me.log(f"receiver: SPEAKING ssrc={data['ssrc']} → user={data['user_id']}")
            if original:
                await original(ws, msg)

        conn.hook = hook
        try:
            from discord.utils import MISSING
            if hasattr(conn, "ws") and conn.ws is not MISSING:
                conn.ws._hook = hook
        except Exception:                              # noqa: BLE001
            pass

    def _on_packet(self, data: bytes) -> None:
        if len(data) < 16:
            return
        if (data[0] >> 6) != 2 or (data[1] & 0x7F) != 0x78:
            return                                     # 不是 RTP voice
        self.packets += 1
        first = data[0]
        _, _, _seq, _ts, ssrc = struct.unpack_from(">BBHII", data, 0)
        if ssrc == self._bot_ssrc:
            return
        cc = first & 0x0F
        has_ext = bool(first & 0x10)
        has_pad = bool(first & 0x20)
        header_len = 12 + 4 * cc + (4 if has_ext else 0)
        if len(data) < header_len + 4:
            return
        ext_len = 0
        if has_ext:
            ext_len = struct.unpack_from(">H", data, 12 + 4 * cc + 2)[0] * 4
        header = bytes(data[:header_len])
        payload = data[header_len:]
        nonce = bytearray(24)
        nonce[:4] = payload[-4:]
        try:
            import nacl.secret
            decrypted = nacl.secret.Aead(self._secret_key).decrypt(
                bytes(payload[:-4]), header, bytes(nonce))
        except Exception:                              # noqa: BLE001
            return
        if ext_len and len(decrypted) > ext_len:
            decrypted = decrypted[ext_len:]
        if has_pad and decrypted:
            pad = decrypted[-1]
            if pad == 0 or pad > len(decrypted):
                return
            decrypted = decrypted[:-pad]
        with self._lock:
            user_id = self._ssrc_to_user.get(ssrc, 0)
        if not user_id:
            user_id = self._fallback_user or self._infer_single_allowed()
            if user_id:
                with self._lock:
                    self._ssrc_to_user[ssrc] = user_id
        if self._dave_session:
            if not user_id:
                return                                 # 沒有 user_id 就不能解 DAVE
            try:
                import davey
                decrypted = self._dave_session.decrypt(user_id, davey.MediaType.audio,
                                                       decrypted)
            except Exception as exc:                   # noqa: BLE001
                if "Unencrypted" not in str(exc):
                    self.log(f"receiver: DAVE 解密失敗 user={user_id}"
                             f"（{str(exc)[:100]}）⇒ 丟棄")
                    return
        if user_id and self.allowed and str(user_id) not in self.allowed:
            return                                     # 白名單外的人整條丟
        if not user_id and self.allowed:
            return                                     # 認不出是誰就不猜
        self._last_pkt = time.monotonic()
        self._tail_sent = 0
        self._tail_user = user_id
        try:
            if ssrc not in self._decoders:
                self._decoders[ssrc] = self._decoder()
            pcm = self._decoders[ssrc].decode(decrypted)
        except Exception:                              # noqa: BLE001
            with self._lock:
                self._decoders.pop(ssrc, None)
            return
        self._count += 1
        if self._count % 500 == 1:
            self.log(f"receiver: 收包 {self._count} ssrc={ssrc} user={user_id or '?'} "
                     f"pcm={len(pcm)}")
        buffer = self._pend[ssrc]
        buffer.extend(pcm)
        while len(buffer) >= BLOCK_BYTES:
            block = bytes(buffer[:BLOCK_BYTES])
            del buffer[:BLOCK_BYTES]
            try:
                self._q.put_nowait((user_id or 0, block))
            except queue.Full:
                self.log("receiver: 佇列滿，丟一塊")

    @staticmethod
    def _decoder():
        import discord
        if not discord.opus.is_loaded():
            load_opus()
        return discord.opus.Decoder()

    # ── 收工 ────────────────────────────────────────────────────────────────
    def stop(self) -> None:
        with self._lock:
            pending = [(ssrc, bytes(buf)) for ssrc, buf in self._pend.items() if buf]
            self._pend.clear()
        for ssrc, raw in pending:
            user = self._ssrc_to_user.get(ssrc, 0)
            if not user:
                continue
            try:
                self._q.put_nowait((user, raw + bytes(BLOCK_BYTES - len(raw))))
            except queue.Full:
                break
        self._stop.set()
        try:
            self.vc._connection.remove_socket_listener(self._on_packet)
        except Exception:                              # noqa: BLE001
            pass
        self._worker.join(timeout=2.0)

    def health(self) -> dict:
        return {"packets": self.packets, "blocks": self.blocks,
                "dave": bool(self._dave_session), "ssrc_users": len(self._ssrc_to_user)}


__all__ = ["BLOCK_BYTES", "VoiceReceiver", "load_opus", "tail_block_due"]
