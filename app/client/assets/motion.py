"""動作片段庫（`bamotion/1`）：`$BLUEAGENT_DATA_DIR/motion/` 下的 `<id>.json` ＋ `index.json`。

規格見 `docs/PLAN-motion-kimodo-20261031.md` §4（片段格式）／§5.1（庫與快取）。
這一層只管「庫裡有哪些片段、這一輪挑哪一個」；**生成**在 GPU 機（Kimodo 服務），**播放**在端末
（`app/client/BlueAgentUnity/Assets/Scripts/Next/Stage3DMotion.cs`）。沒有庫＝沒有動作，聊天照常。
"""

from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import struct
import threading

#: 片段格式版本（前後端契約；改語意要一起改）
FORMAT = "bamotion/1"

#: 契約要求的 23 個身體關節（可以多給，不能少給）
BODY_JOINTS = (
    "Hips", "Spine1", "Spine2", "Chest", "Neck1", "Neck2", "Head",
    "LeftShoulder", "LeftArm", "LeftForeArm", "LeftHand",
    "RightShoulder", "RightArm", "RightForeArm", "RightHand",
    "LeftLeg", "LeftShin", "LeftFoot", "LeftToeBase",
    "RightLeg", "RightShin", "RightFoot", "RightToeBase",
)


def _b64(raw) -> bytes:
    try:
        return base64.b64decode(str(raw or ""), validate=False)
    except Exception:                                    # noqa: BLE001 — 壞檔就當沒有
        return b""


def clip_digest(rot_raw: bytes, hips_raw: bytes, fps: int) -> str:
    """`id`＝內容雜湊（`rotations` ＋ `hips` ＋ `<i` 的 fps）前 16 個十六進位字。

    與 `app/client/tools/motion_clip.py` 的算法逐位元一致（同輸入同 id ⇒ 端末可以拿它當 ETag）。
    """
    return hashlib.sha256(rot_raw + hips_raw + struct.pack("<i", int(fps))).hexdigest()[:16]


def problems(clip: dict) -> list[str]:
    """片段格式自檢（回空清單＝合格）。部署端預生成時用它把壞片段擋在庫外。"""
    errors: list[str] = []
    if not isinstance(clip, dict):
        return ["不是物件"]
    if clip.get("format") != FORMAT:
        errors.append(f"format 應為 {FORMAT}")
    fps, frames = clip.get("fps"), clip.get("frames")
    if not isinstance(fps, int) or fps <= 0:
        errors.append("fps 要是正整數")
        return errors
    if not isinstance(frames, int) or frames <= 0:
        errors.append("frames 要是正整數")
        return errors
    names = clip.get("joints")
    if not isinstance(names, list) or len(names) == 0:
        errors.append("joints 要是非空陣列")
        return errors
    missing = [name for name in BODY_JOINTS if name not in names]
    if missing:
        errors.append("缺關節：" + "、".join(missing))
    rot = _b64(clip.get("rotations"))
    hips = _b64(clip.get("hips"))
    if len(rot) != frames * len(names) * 16:
        errors.append(f"rotations 長度 {len(rot)} ≠ frames×joints×16")
    if len(hips) != frames * 12:
        errors.append(f"hips 長度 {len(hips)} ≠ frames×3×4")
    if not errors:
        values = struct.unpack(f"<{len(rot) // 4}f", rot)
        worst = 0.0
        for i in range(0, len(values), 4):
            norm = (values[i] ** 2 + values[i + 1] ** 2 + values[i + 2] ** 2 + values[i + 3] ** 2) ** 0.5
            worst = max(worst, abs(norm - 1.0))
        if worst > 1e-3:
            errors.append(f"四元數不是單位長（最大偏差 {worst:.4f}）")
        digest = clip_digest(rot, hips, fps)
        if clip.get("id") != digest:
            errors.append(f"id 應為內容雜湊 {digest}")
    height = clip.get("hipsRestHeight")
    if not isinstance(height, (int, float)) or height <= 0:
        errors.append("hipsRestHeight 要是正數")
    return errors


class MotionLibrary:
    """片段庫（唯讀）：`<id>.json` ＋ `index.json`（`{"tags": {"<tag>": ["<id>", …]}}`）。

    `index.json` 不見了不致命：直接掃目錄、按每段自己的 `tag` 欄位推導索引（部署端忘了跑索引也能用）。
    """

    def __init__(self, root) -> None:
        self.root = pathlib.Path(root)
        self._lock = threading.Lock()
        self._stamp: tuple = ()
        self._clips: dict[str, dict] = {}
        self._tags: dict[str, list[str]] = {}
        self._last: dict[str, str] = {}          # entity → 上一輪用過的 id（同 tag 不連兩輪用同一個）

    # ── 讀 ───────────────────────────────────────────────────────────────
    def _directory_stamp(self) -> tuple:
        try:
            stat = self.root.stat()
        except OSError:
            return ()
        index = self.root / "index.json"
        try:
            index_stat = index.stat()
            tail = (index_stat.st_mtime_ns, index_stat.st_size)
        except OSError:
            tail = (0, 0)
        return (stat.st_mtime_ns, tail)

    def _load(self) -> None:
        stamp = self._directory_stamp()
        if stamp == self._stamp:
            return
        clips: dict[str, dict] = {}
        tags: dict[str, list[str]] = {}
        try:
            files = sorted(self.root.glob("*.json"))
        except OSError:
            files = []
        for path in files:
            if path.name == "index.json":
                continue
            try:
                clip = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            clip_id = str(clip.get("id") or path.stem)
            clip["id"] = clip_id
            clips[clip_id] = clip
            tag = str(clip.get("tag") or "").strip()
            if tag:
                tags.setdefault(tag, []).append(clip_id)
        # `index.json` 有就以它為準（部署端可以指定播放順序），但只認庫裡真的有的 id
        try:
            index = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            index = {}
        ordered: set[str] = set()
        for tag, ids in (index.get("tags") or {}).items():
            known = [str(i) for i in ids if str(i) in clips]
            if known:
                tags[str(tag)] = known
                ordered.add(str(tag))
        # 掃目錄得到的索引按 id 排（穩定、可重現）；`index.json` 給的順序是**播放順序**，照它
        for tag_name, ids in tags.items():
            if tag_name not in ordered:
                ids.sort()
        self._clips, self._tags, self._stamp = clips, tags, stamp

    def reload(self) -> None:
        with self._lock:
            self._stamp = ()
            self._load()

    # ── 查 ───────────────────────────────────────────────────────────────
    def get(self, clip_id: str) -> dict | None:
        with self._lock:
            self._load()
            return self._clips.get(str(clip_id or "").strip())

    def tags(self) -> dict[str, list[str]]:
        with self._lock:
            self._load()
            return {tag: list(ids) for tag, ids in self._tags.items()}

    def count(self) -> int:
        with self._lock:
            self._load()
            return len(self._clips)

    # ── 挑（§5.2）─────────────────────────────────────────────────────────
    def pick(self, tag: str, *, entity: str = "", avoid: str = "") -> dict | None:
        """同一個 tag 有 2–3 個 seed 時輪流挑；`avoid` 是上一輪用過的 id（不連兩輪相同）。"""
        with self._lock:
            self._load()
            ids = list(self._tags.get(str(tag or "").strip(), ()))
            if not ids:
                return None
            previous = avoid or self._last.get(entity, "")
            choice = ids[0]
            if len(ids) > 1 and ids[0] == previous:
                choice = ids[1]
            if entity:
                self._last[entity] = choice
            return self._clips.get(choice)


__all__ = ["BODY_JOINTS", "FORMAT", "MotionLibrary", "clip_digest", "problems"]
