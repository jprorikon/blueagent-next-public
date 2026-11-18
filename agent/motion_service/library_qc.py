"""動作片段的品質判據（`bamotion/1`）：哪些 tag 該由哪個部位主導、幅度要在什麼範圍。

**這一份是唯一真源**：`tools/_motion_lib_qc.py`（檢查既有庫）與
`motion_service/rebuild_library_qc.py`（生不出合格片段就一直換 seed）都 import 這裡，
不要兩邊各寫一份門檻。

量的是「**幅度**」不是「抖動」——用相鄰幀夾角累加會讓手／前臂的細碎抖動主導，
每個 tag 都會誤判成「手在動」（第一版踩過）。所以：
  幅度 = 整段裡離第一幀最遠的那一幀差幾度；
  抖動 = 相鄰幀夾角的平均（只當參考，不當判據）。
"""

from __future__ import annotations

import base64
import math
import struct

#: 23 關節的父子關係（只為了把「世界旋轉」換算成「相對父關節的旋轉」）。
#: ⚠️ 為什麼一定要換：clip 存的是 T-pose 相對的**世界**旋轉，深鞠躬時軀幹轉 100°，
#: 頭的「世界」旋轉也跟著轉 100° ⇒ 直接比世界旋轉會把「頭跟著軀幹」誤判成「頭在自己轉」
#: （第一版對 bow 全滅、對 sad 誤報就是這個原因）。
PARENT = {
    "Spine1": "Hips", "Spine2": "Spine1", "Chest": "Spine2",
    "Neck1": "Chest", "Neck2": "Neck1", "Head": "Neck2",
    "LeftShoulder": "Chest", "LeftArm": "LeftShoulder", "LeftForeArm": "LeftArm", "LeftHand": "LeftForeArm",
    "RightShoulder": "Chest", "RightArm": "RightShoulder", "RightForeArm": "RightArm", "RightHand": "RightForeArm",
    "LeftLeg": "Hips", "LeftShin": "LeftLeg", "LeftFoot": "LeftShin", "LeftToeBase": "LeftFoot",
    "RightLeg": "Hips", "RightShin": "RightLeg", "RightFoot": "RightShin", "RightToeBase": "RightFoot",
}

#: 每個 tag 允許的「主導關節」（SOMA 23 關節命名）
EXPECTED = {
    "nod": {"Neck1", "Neck2", "Head"},
    "shake": {"Neck1", "Neck2", "Head"},
    "bow": {"Spine1", "Spine2", "Chest", "Hips", "Neck1", "Neck2", "Head",
            "LeftArm", "RightArm", "LeftForeArm", "RightForeArm", "LeftHand", "RightHand"},
    "greet": {"LeftArm", "RightArm", "LeftForeArm", "RightForeArm",
              "LeftHand", "RightHand", "LeftShoulder", "RightShoulder"},
    "cheer": {"LeftArm", "RightArm", "LeftForeArm", "RightForeArm",
              "LeftHand", "RightHand", "LeftShoulder", "RightShoulder"},
    "point": {"LeftArm", "RightArm", "LeftForeArm", "RightForeArm",
              "LeftHand", "RightHand", "LeftShoulder", "RightShoulder"},
    "laugh": {"Spine2", "Chest", "Neck1", "Neck2", "Head", "Hips",
              "LeftArm", "RightArm", "LeftForeArm", "RightForeArm", "LeftHand", "RightHand"},
    "shy": {"Neck1", "Neck2", "Head", "Chest", "Spine2",
            "LeftForeArm", "RightForeArm", "LeftHand", "RightHand"},
    "think": {"Neck1", "Neck2", "Head", "Chest", "LeftArm", "RightArm",
              "LeftForeArm", "RightForeArm", "LeftHand", "RightHand"},
    "surprised": {"Chest", "Spine2", "Head", "Neck1", "Neck2",
                  "LeftArm", "RightArm", "LeftForeArm", "RightForeArm", "LeftHand", "RightHand"},
    "shrug": {"LeftShoulder", "RightShoulder", "LeftArm", "RightArm",
              "LeftForeArm", "RightForeArm", "LeftHand", "RightHand", "Chest"},
    "sad": {"Spine1", "Spine2", "Chest", "Neck1", "Neck2", "Head"},
}

#: 幅度帶按**關節部位**給：片段是 T-pose 相對旋轉，手臂從自然下垂到舉起本來就接近 180°
BAND_BY_FAMILY = {
    "arm": (25.0, 180.0),
    "head": (8.0, 70.0),
    "spine": (6.0, 60.0),
    "leg": (0.0, 35.0),
}

#: 個別 tag 對某些部位的幅度覆蓋：深鞠躬時頭／頸相對自己的第一幀本來就會轉很多（實測 110–180°），
#: 這不是缺陷。只在「主導部位屬於該 tag 的合理演出」時放寬。
BAND_OVERRIDES = {
    # ⚠️ bow 的頭不要放寬：實測放寬到 180° 會被「頭整個翻過去」的片段混進來（Head 179.7°），
    #    寧可生不出來，也不要收一段壞的（owner 2026-10-31 QC）。
    ("bow", "spine"): (6.0, 110.0),
    ("laugh", "head"): (8.0, 90.0),
    ("surprised", "head"): (8.0, 95.0),
    ("shy", "head"): (8.0, 95.0),
}

#: tag 表上的秒數（與 `build_library.TAGS` 一致）
SECONDS = {"greet": 3, "nod": 2, "shake": 2, "bow": 3, "laugh": 3, "shy": 4,
           "think": 4, "cheer": 3, "surprised": 2, "shrug": 2, "sad": 4, "point": 3}


def family(joint: str) -> str:
    if joint.endswith(("Arm", "ForeArm", "Hand", "Shoulder")):
        return "arm"
    if joint.startswith(("Head", "Neck")):
        return "head"
    if joint.startswith(("Spine", "Chest", "Hips")):
        return "spine"
    return "leg"


def _quats(clip: dict) -> list[list[tuple[float, float, float, float]]]:
    """把 base64 的 float32 四元數解成 [frame][joint]（x, y, z, w）。"""
    frames = int(clip["frames"])
    joints = len(clip["joints"])
    raw = base64.b64decode(clip["rotations"])
    values = struct.unpack("<%df" % (frames * joints * 4), raw)
    out = []
    for t in range(frames):
        base = t * joints * 4
        out.append([tuple(values[base + j * 4: base + j * 4 + 4]) for j in range(joints)])
    return out


def _q_inv(q):
    x, y, z, w = q
    return (-x, -y, -z, w)


def _q_mul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def _angle(a, b) -> float:
    dot = abs(sum(x * y for x, y in zip(a, b)))
    dot = 1.0 if dot > 1.0 else dot
    return math.degrees(2.0 * math.acos(dot))


def amplitudes(clip: dict, *, local: bool = True) -> dict[str, float]:
    """每個關節「**相對父關節**」的旋轉，離第一幀最遠差幾度。

    local=False 時退回世界旋轉（只給除錯用：軀幹一彎，頭的世界旋轉會跟著大，
    拿它判「哪個部位在做動作」會誤判）。
    """
    quats = _quats(clip)
    joints = clip["joints"]
    position = {name: index for index, name in enumerate(joints)}
    reference = quats[0]
    result: dict[str, float] = {}
    for index, name in enumerate(joints):
        parent = PARENT.get(name)
        best = 0.0
        for frame in quats:
            if local and parent in position:
                parent_index = position[parent]
                current = _q_mul(_q_inv(frame[parent_index]), frame[index])
                base = _q_mul(_q_inv(reference[parent_index]), reference[index])
            else:
                current, base = frame[index], reference[index]
            best = max(best, _angle(current, base))
        result[name] = best
    return result


def jitter(clip: dict) -> float:
    """相鄰幀夾角的平均（度／幀）——只用來觀察，不當判據。"""
    quats = _quats(clip)
    joints = len(clip["joints"])
    total = 0.0
    count = 0
    for t in range(1, len(quats)):
        for j in range(joints):
            dot = abs(sum(a * b for a, b in zip(quats[t][j], quats[t - 1][j])))
            dot = 1.0 if dot > 1.0 else dot
            total += math.degrees(2.0 * math.acos(dot))
            count += 1
    return total / max(1, count)


#: 整體運動量下限（度）：全關節、世界旋轉、相對第一幀的最大值。
#: 低於這個數字 ⇒ 畫面上幾乎看不到（實測 sad 有 2.7°、shake 有 5° 的片段）。
ENERGY_MIN = {"greet": 70, "cheer": 80, "point": 60, "bow": 30, "laugh": 25,
              "shy": 20, "think": 25, "surprised": 25, "shrug": 25, "sad": 8,
              "nod": 12, "shake": 12}

#: 這幾個 tag 不該由腿主導（腿一動就不是那個情緒了）
NO_LEG = {"greet", "nod", "shake", "bow", "laugh", "shy", "think", "cheer",
          "surprised", "shrug", "sad", "point"}


def energy(clip: dict) -> float:
    """整體運動量：所有關節裡「世界旋轉」相對第一幀的最大角度。"""
    return max(amplitudes(clip, local=False).values())


def check_clip(clip: dict) -> list[str]:
    """回問題清單（空＝合格）。

    只留兩條站得住的判據（主導關節那條在 T-pose 骨架下不可靠，已移除）：
      ① 時長 = tag 表上的秒數；
      ② 整體運動量要夠大（不然畫面上看不到動作）。
    """
    problems: list[str] = []
    tag = str(clip.get("tag") or "")
    frames, fps = int(clip["frames"]), int(clip["fps"])
    duration = frames / max(1, fps)
    expected_seconds = SECONDS.get(tag, duration)
    if abs(duration - expected_seconds) > 0.1:
        problems.append(f"時長 {duration:.2f}s ≠ {expected_seconds}s")
    amount = energy(clip)
    minimum = ENERGY_MIN.get(tag, 10)
    if amount < minimum:
        problems.append(f"運動量 {amount:.1f}° 太小（<{minimum}°，畫面上看不到）")
    return problems


def summary(clip: dict) -> dict:
    local = amplitudes(clip, local=True)
    dominant = max(local, key=lambda name: local[name])
    return {"dominant": dominant, "peak": round(energy(clip), 1),
            "local_peak": round(local[dominant], 1), "jitter": round(jitter(clip), 3)}
