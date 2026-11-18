#!/usr/bin/env python3
"""`bamotion/1` 動作片段：參考編碼器、驗證器、合成示範（owner 2026-10-31：Kimodo 驅動 3D 立繪）。

格式規格在 `docs/PLAN-motion-kimodo-20261031.md` §4。這支是**兩邊共用的參考實作**：
- 部署端（GPU 機的 Kimodo 服務）用 `from_kimodo_npz()` 把 Kimodo 的 NPZ 轉成片段，並跑 `check_tpose_relative()` 自檢；
- 前端用 `demo` 產生合成片段（不必有 GPU），驗證 Unity 的重定向（`Stage3DMotion.cs`）。

座標＝SOMA 原生：右手系、Y 上、+Z 前（角色面向 +Z、角色的左邊在 +X）；旋轉＝各關節**世界旋轉、相對 T-pose**。

用法：
  python3 tools/motion_clip.py demo <輸出目錄>                         # 合成 greet／bow／nod／cheer（純 Python）
  python3 tools/motion_clip.py validate <clip.json> [...]
  python3 tools/motion_clip.py from-npz <kimodo.npz> <somaskel77_standard_tpose.bvh> \\
          --tag greet --prompt "..." --seed 7 -o clip.json                 # 要 numpy（部署端）
"""
import argparse
import base64
import hashlib
import json
import math
import pathlib
import struct
import sys

FORMAT = "bamotion/1"

#: 片段裡一定要有的 23 個身體關節（SOMA 命名；順序＝片段裡的順序）
BODY_JOINTS = [
    "Hips", "Spine1", "Spine2", "Chest", "Neck1", "Neck2", "Head",
    "LeftShoulder", "LeftArm", "LeftForeArm", "LeftHand",
    "RightShoulder", "RightArm", "RightForeArm", "RightHand",
    "LeftLeg", "LeftShin", "LeftFoot", "LeftToeBase",
    "RightLeg", "RightShin", "RightFoot", "RightToeBase",
]

#: 合成示範用的父子關係（與 somaskel77 的身體部分相同）
PARENT = {
    "Hips": None, "Spine1": "Hips", "Spine2": "Spine1", "Chest": "Spine2",
    "Neck1": "Chest", "Neck2": "Neck1", "Head": "Neck2",
    "LeftShoulder": "Chest", "LeftArm": "LeftShoulder", "LeftForeArm": "LeftArm", "LeftHand": "LeftForeArm",
    "RightShoulder": "Chest", "RightArm": "RightShoulder", "RightForeArm": "RightArm", "RightHand": "RightForeArm",
    "LeftLeg": "Hips", "LeftShin": "LeftLeg", "LeftFoot": "LeftShin", "LeftToeBase": "LeftFoot",
    "RightLeg": "Hips", "RightShin": "RightLeg", "RightFoot": "RightShin", "RightToeBase": "RightFoot",
}

HIPS_REST_HEIGHT = 1.0      # SOMA 標準 T-pose 的 Hips 高（米；BVH 裡是 100 cm）


# ── 四元數（x, y, z, w；右手系）─────────────────────────────────────────────

def q_mul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def q_axis(axis, degrees):
    x, y, z = axis
    n = math.sqrt(x * x + y * y + z * z)
    half = math.radians(degrees) * 0.5
    s = math.sin(half) / n
    return (x * s, y * s, z * s, math.cos(half))


def q_rotate(q, v):
    x, y, z, w = q
    vx, vy, vz = v
    # v' = q v q*
    tx = 2 * (y * vz - z * vy)
    ty = 2 * (z * vx - x * vz)
    tz = 2 * (x * vy - y * vx)
    return (vx + w * tx + (y * tz - z * ty),
            vy + w * ty + (z * tx - x * tz),
            vz + w * tz + (x * ty - y * tx))


def q_from_matrix(m):
    """3×3 旋轉矩陣（列優先的巢狀 list）→ 四元數 (x, y, z, w)。"""
    m00, m01, m02 = m[0]
    m10, m11, m12 = m[1]
    m20, m21, m22 = m[2]
    trace = m00 + m11 + m22
    if trace > 0:
        s = math.sqrt(trace + 1.0) * 2
        q = ((m21 - m12) / s, (m02 - m20) / s, (m10 - m01) / s, 0.25 * s)
    elif m00 > m11 and m00 > m22:
        s = math.sqrt(1.0 + m00 - m11 - m22) * 2
        q = (0.25 * s, (m01 + m10) / s, (m02 + m20) / s, (m21 - m12) / s)
    elif m11 > m22:
        s = math.sqrt(1.0 + m11 - m00 - m22) * 2
        q = ((m01 + m10) / s, 0.25 * s, (m12 + m21) / s, (m02 - m20) / s)
    else:
        s = math.sqrt(1.0 + m22 - m00 - m11) * 2
        q = ((m02 + m20) / s, (m12 + m21) / s, 0.25 * s, (m10 - m01) / s)
    n = math.sqrt(sum(c * c for c in q))
    return tuple(c / n for c in q)


# ── 編碼／驗證 ──────────────────────────────────────────────────────────────

def _pack(values):
    return struct.pack("<%df" % len(values), *values)


def encode(rotations, hips, fps, meta):
    """rotations: [T][J][4]；hips: [T][3]（皆為 SOMA 座標）。回 `bamotion/1` dict（`id`＝內容雜湊）。"""
    frames = len(rotations)
    if frames == 0 or len(hips) != frames:
        raise ValueError("rotations／hips 幀數不一致或為 0")
    flat_rot = [c for frame in rotations for q in frame for c in q]
    flat_hips = [c for p in hips for c in p]
    rot_bytes, hips_bytes = _pack(flat_rot), _pack(flat_hips)
    digest = hashlib.sha256(rot_bytes + hips_bytes + struct.pack("<i", int(fps))).hexdigest()[:16]
    clip = {
        "format": FORMAT,
        "id": digest,
        "tag": meta.get("tag", ""),
        "prompt": meta.get("prompt", ""),
        "model": meta.get("model", ""),
        "seed": meta.get("seed", 0),
        "fps": int(fps),
        "frames": frames,
        "joints": list(BODY_JOINTS),
        "rotations": base64.b64encode(rot_bytes).decode("ascii"),
        "hips": base64.b64encode(hips_bytes).decode("ascii"),
        "hipsRestHeight": float(meta.get("hipsRestHeight", HIPS_REST_HEIGHT)),
    }
    return clip


def validate(clip):
    """回錯誤字串的 list（空＝通過）。"""
    errors = []
    if clip.get("format") != FORMAT:
        errors.append(f"format 應為 {FORMAT}")
    frames, fps = clip.get("frames", 0), clip.get("fps", 0)
    joints = clip.get("joints") or []
    missing = [j for j in BODY_JOINTS if j not in joints]
    if missing:
        errors.append(f"缺關節：{missing}")
    if not (1 <= frames <= 300):
        errors.append(f"frames={frames}（應 1–300）")
    if fps != 30:
        errors.append(f"fps={fps}（Kimodo 是 30）")
    try:
        rot = base64.b64decode(clip.get("rotations", ""))
        hips = base64.b64decode(clip.get("hips", ""))
    except Exception as error:      # noqa: BLE001 — 驗證器回報即可
        return errors + [f"base64 解不開：{error}"]
    if len(rot) != frames * len(joints) * 16:
        errors.append(f"rotations 長度 {len(rot)} ≠ frames×joints×16")
    if len(hips) != frames * 12:
        errors.append(f"hips 長度 {len(hips)} ≠ frames×12")
    if not errors:
        values = struct.unpack("<%df" % (len(rot) // 4), rot)
        worst = max(abs(math.sqrt(sum(values[i + k] ** 2 for k in range(4))) - 1.0) for i in range(0, len(values), 4))
        if worst > 1e-3:
            errors.append(f"四元數不是單位長（最大偏差 {worst:.4f}）")
        digest = hashlib.sha256(rot + hips + struct.pack("<i", int(fps))).hexdigest()[:16]
        if clip.get("id") != digest:
            errors.append(f"id 應為內容雜湊 {digest}")
    if not isinstance(clip.get("hipsRestHeight"), (int, float)) or clip["hipsRestHeight"] <= 0:
        errors.append("hipsRestHeight 要是正數")
    return errors


# ── Kimodo NPZ → 片段（部署端；要 numpy）─────────────────────────────────────

def bvh_joint_names(bvh_path):
    """從 somaskel77 的 T-pose BVH 讀關節名（出現順序；含 Root、不含 End Site）。"""
    names = []
    for line in pathlib.Path(bvh_path).read_text(encoding="utf-8").splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] in ("ROOT", "JOINT"):
            names.append(parts[1])
    return names


def npz_joint_names(npz, bvh_path):
    """NPZ 的 J 對到哪些名字：J＝BVH 關節數 ⇒ 含 Root；J＝BVH 關節數 − 1 ⇒ 不含 Root。其他 ⇒ 拒絕。"""
    names = bvh_joint_names(bvh_path)
    count = npz["global_rot_mats"].shape[1]
    if count == len(names):
        return names
    if count == len(names) - 1 and names and names[0] == "Root":
        return names[1:]
    raise SystemExit(f"NPZ 有 {count} 個關節、BVH 有 {len(names)} 個 ⇒ 對不上，先查 Kimodo 的關節順序")


def check_tpose_relative(npz, names, tolerance_deg=10.0):
    """
    必做自檢：`global_rot_mats` 是不是「相對 T-pose 的世界旋轉」。
    T-pose 裡左上臂朝 +X、右上臂朝 −X ⇒ R·(±X) 應與 posed_joints 量出的上臂方向一致（每一幀）。
    """
    import numpy as np
    index = {name: i for i, name in enumerate(names)}
    rot, pos = npz["global_rot_mats"], npz["posed_joints"]
    worst = 0.0
    for arm, fore, axis in (("LeftArm", "LeftForeArm", (1, 0, 0)), ("RightArm", "RightForeArm", (-1, 0, 0))):
        a, f = index[arm], index[fore]
        for t in range(rot.shape[0]):
            predicted = rot[t, a] @ np.array(axis, dtype=float)
            measured = pos[t, f] - pos[t, a]
            cos = float(predicted @ measured / (np.linalg.norm(predicted) * np.linalg.norm(measured) + 1e-9))
            worst = max(worst, math.degrees(math.acos(max(-1.0, min(1.0, cos)))))
    if worst > tolerance_deg:
        raise SystemExit(f"✗ 自檢失敗：上臂方向最大誤差 {worst:.1f}°（> {tolerance_deg}°）⇒ global_rot_mats 不是相對 T-pose，別上線")
    print(f"✓ 自檢通過：上臂方向最大誤差 {worst:.2f}°")
    return worst


def from_kimodo_npz(npz_path, bvh_path, meta):
    import numpy as np
    npz = np.load(npz_path)
    names = npz_joint_names(npz, bvh_path)
    check_tpose_relative(npz, names)
    index = {name: i for i, name in enumerate(names)}
    missing = [j for j in BODY_JOINTS if j not in index]
    if missing:
        raise SystemExit(f"NPZ 裡沒有這些身體關節：{missing}")
    mats = npz["global_rot_mats"]
    hips = npz["root_positions"]
    rotations = [[q_from_matrix(mats[t, index[j]].tolist()) for j in BODY_JOINTS] for t in range(mats.shape[0])]
    meta = dict(meta)
    meta.setdefault("model", "Kimodo-SOMA-RP-v1.2")
    return encode(rotations, hips.tolist(), 30, meta)


# ── 合成示範（純 Python；驗前端重定向用，不是正式動作）──────────────────────

def _ease(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def _arms_down(local):
    """T-pose → 手臂垂下：左臂（+X）繞 Z −75°、右臂（−X）繞 Z +75°。"""
    local["LeftArm"] = q_axis((0, 0, 1), -75)
    local["RightArm"] = q_axis((0, 0, 1), 75)


def _compose(local):
    """局部旋轉（相對父骨）→ 世界旋轉（相對 T-pose）。T-pose 的局部全是 identity。"""
    world = {}
    for joint in BODY_JOINTS:
        q = local.get(joint, (0.0, 0.0, 0.0, 1.0))
        parent = PARENT[joint]
        world[joint] = q if parent is None else q_mul(world[parent], q)
    return [world[j] for j in BODY_JOINTS]


def _demo(name, seconds, pose_at):
    frames = int(seconds * 30)
    rotations, hips = [], []
    for f in range(frames):
        t = f / 30.0
        local, lift = pose_at(t, seconds)
        rotations.append(_compose(local))
        hips.append((0.0, HIPS_REST_HEIGHT + lift, 0.0))
    return encode(rotations, hips, 30, {"tag": name, "prompt": f"synthetic {name}", "model": "synthetic"})


def demo_greet():
    """**右手**揮手（角色自己的右手＝−X 那側）——驗左右有沒有對反。"""
    def pose(t, total):
        local = {}
        _arms_down(local)
        w = _ease(t / 0.4) * _ease((total - t) / 0.4)
        local["RightArm"] = q_axis((0, 0, 1), 75 + (-35 - 75) * w)          # 垂下 → 斜上
        local["RightForeArm"] = q_axis((0, 0, 1), (-60 + 22 * math.sin(t * 2 * math.pi * 2.2)) * w)
        local["Head"] = q_axis((0, 0, 1), 6 * w)                               # 頭微微歪向揮手那側
        return local, 0.0
    return _demo("greet", 3.0, pose)


def demo_bow():
    """鞠躬：脊椎往**前**彎（繞 +X 正角度 ⇒ 上方往 +Z 倒）——驗前後有沒有對反。"""
    def pose(t, total):
        local = {}
        _arms_down(local)
        w = _ease(t / 0.8) * _ease((total - t) / 0.9)
        local["Spine1"] = q_axis((1, 0, 0), 18 * w)
        local["Chest"] = q_axis((1, 0, 0), 14 * w)
        local["Head"] = q_axis((1, 0, 0), 12 * w)
        return local, -0.02 * w
    return _demo("bow", 3.0, pose)


def demo_nod():
    def pose(t, total):
        local = {}
        _arms_down(local)
        local["Head"] = q_axis((1, 0, 0), 14 * max(0.0, math.sin(t * 2 * math.pi * 1.4)) * _ease((total - t) / 0.3))
        return local, 0.0
    return _demo("nod", 2.0, pose)


def demo_cheer():
    """雙手舉高＋小跳：驗雙臂與 Hips 位移。"""
    def pose(t, total):
        local = {}
        w = _ease(t / 0.4) * _ease((total - t) / 0.5)
        local["LeftArm"] = q_axis((0, 0, 1), -75 + (70 + 75) * w)
        local["RightArm"] = q_axis((0, 0, 1), 75 + (-70 - 75) * w)
        local["LeftForeArm"] = q_axis((0, 0, 1), 15 * w)
        local["RightForeArm"] = q_axis((0, 0, 1), -15 * w)
        hop = 0.06 * max(0.0, math.sin(t * 2 * math.pi * 1.6)) * w
        return local, hop
    return _demo("cheer", 3.0, pose)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p_demo = sub.add_parser("demo")
    p_demo.add_argument("out")
    p_val = sub.add_parser("validate")
    p_val.add_argument("clips", nargs="+")
    p_npz = sub.add_parser("from-npz")
    p_npz.add_argument("npz")
    p_npz.add_argument("bvh")
    p_npz.add_argument("--tag", default="")
    p_npz.add_argument("--prompt", default="")
    p_npz.add_argument("--seed", type=int, default=0)
    p_npz.add_argument("-o", "--output", required=True)
    args = parser.parse_args()

    if args.command == "demo":
        out = pathlib.Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for build in (demo_greet, demo_bow, demo_nod, demo_cheer):
            clip = build()
            problems = validate(clip)
            if problems:
                raise SystemExit(f"{clip['tag']}：{problems}")
            (out / f"{clip['tag']}.json").write_text(json.dumps(clip), encoding="utf-8")
            print(f"✓ {clip['tag']}：{clip['frames']} 幀 → {out / (clip['tag'] + '.json')}（id {clip['id']}）")
    elif args.command == "validate":
        bad = 0
        for path in args.clips:
            problems = validate(json.loads(pathlib.Path(path).read_text(encoding="utf-8")))
            print(("✓ " if not problems else "✗ ") + path + ("" if not problems else "：" + "；".join(problems)))
            bad += bool(problems)
        sys.exit(1 if bad else 0)
    else:
        clip = from_kimodo_npz(args.npz, args.bvh, {"tag": args.tag, "prompt": args.prompt, "seed": args.seed})
        problems = validate(clip)
        if problems:
            raise SystemExit("；".join(problems))
        pathlib.Path(args.output).write_text(json.dumps(clip), encoding="utf-8")
        print(f"✓ {args.output}（id {clip['id']}，{clip['frames']} 幀）")


if __name__ == "__main__":
    main()
