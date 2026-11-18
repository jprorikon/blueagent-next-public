#!/usr/bin/env python3
"""MAGIC 待機的訓練資料（`magic-train/1`）——`docs/PLAN-magic-motion-20261031.md` §6.4／§7。

輸出 `app/client/BlueAgentUnity/Assets/Resources/models3d/magic-idle.json`：
7 軸角度（度，繞角色自己的軸；X＝右／前後彎、Y＝上／左右轉、Z＝前／左右歪），30 Hz。
端末載入後當場擬合 VAR（`BlueAgent.Logic.Motion.VarModel`），生成不重複的待機微動作。

    # 首選：在 GPU 機那一側叫 Kimodo 生成待機片段，再轉成 7 軸（需要服務 URL／token，不寫進倉）
    python3 app/client/tools/magic_idle.py kimodo --url http://127.0.0.1:8790 --token *** \\
        --clips-dir data/motion-idle --out app/client/BlueAgentUnity/Assets/Resources/models3d/magic-idle.json

    # 已經有 bamotion/1 片段：只做轉換
    python3 app/client/tools/magic_idle.py from-clips data/motion-idle/*.json --out …/magic-idle.json

    # 佔位：程序化合成（Kimodo 待機資料到位前，讓端末整條路先通）
    python3 app/client/tools/magic_idle.py synthetic --seconds 90 --seed 7 --out …/magic-idle.json

只用標準庫（不需要 numpy）。
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import pathlib
import random
import struct
import sys
import urllib.request

AXES = ("bodyX", "bodyY", "bodyZ", "chestX", "headX", "headY", "headZ")
FORMAT = "magic-train/1"
RATE = 30

#: Kimodo 待機 prompt（英文；原地、不走路、不做語意手勢）。每個 prompt 生成 `--seeds` 段 × `--seconds` 秒
#: 待機用的提示詞：**刻意不含「轉頭／看四周／大幅擺動」**——那些會讓待機出現大位移，
#: 變成「忽然換了一個姿勢」（owner 2026-10-31「待機而已，姿勢可以更少換」）。
IDLE_PROMPTS = (
    "A girl stands idle in place, breathing calmly, hands relaxed in front of her.",
    "A person stands still and slowly shifts their weight from one leg to the other, without turning.",
    "A girl stands quietly in place, small relaxed shoulder movements, head steady.",
)

# ── 四元數（x, y, z, w）────────────────────────────────────────────────────────


def q_mul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def q_inv(q):
    x, y, z, w = q
    return (-x, -y, -z, w)


def rotation_vector_deg(q):
    """單位四元數 → 旋轉向量（軸 × 角度，度）。取短的那一邊（w ≥ 0）。"""
    x, y, z, w = q
    if w < 0:
        x, y, z, w = -x, -y, -z, -w
    norm = math.sqrt(x * x + y * y + z * z)
    if norm < 1e-12:
        return (math.degrees(2 * x), math.degrees(2 * y), math.degrees(2 * z))
    angle = math.degrees(2 * math.atan2(norm, w))
    return (x / norm * angle, y / norm * angle, z / norm * angle)


# ── bamotion/1 → 7 軸 ─────────────────────────────────────────────────────────


def decode_clip(clip: dict):
    """回 `(fps, {joint: [q_unity per frame]})`。座標換成 Unity（鏡射 X：(x, −y, −z, w)），同 `MotionClip.Parse`。"""
    if clip.get("format") != "bamotion/1":
        raise ValueError(f"not bamotion/1: {clip.get('format')}")
    joints = list(clip["joints"])
    frames = int(clip["frames"])
    raw = base64.b64decode(clip["rotations"])
    values = struct.unpack(f"<{frames * len(joints) * 4}f", raw)
    out = {name: [] for name in joints}
    for f in range(frames):
        for j, name in enumerate(joints):
            at = (f * len(joints) + j) * 4
            x, y, z, w = values[at:at + 4]
            out[name].append((x, -y, -z, w))
    return int(clip.get("fps", RATE)), out


def axes_from_clip(clip: dict) -> list[list[float]]:
    """
    body＝`Spine1` 的世界差量（含骨盆：重心、整體晃）；
    chestX＝`Chest` 相對 `Spine1` 的前後彎（呼吸）；
    head＝`Head` 相對 `Chest`（頸＋頭合起來；端末再分給 Neck／Head）。
    """
    fps, joints = decode_clip(clip)
    if fps != RATE:
        raise ValueError(f"片段是 {fps} fps，這裡只收 {RATE}（Kimodo 預設 30）")
    rows = []
    for spine, chest, head in zip(joints["Spine1"], joints["Chest"], joints["Head"]):
        body = rotation_vector_deg(spine)
        chest_local = rotation_vector_deg(q_mul(q_inv(spine), chest))
        head_local = rotation_vector_deg(q_mul(q_inv(chest), head))
        rows.append([body[0], body[1], body[2], chest_local[0], head_local[0], head_local[1], head_local[2]])
    return rows


def highpass(rows: list[list[float]], window_s: float) -> list[list[float]]:
    """減掉移動平均（視窗 `window_s` 秒）：去掉整段慢慢轉向／前傾這種漂移，留下幾秒內的重心轉移與微動作。"""
    half = max(1, int(window_s * RATE / 2))
    out = []
    for i in range(len(rows)):
        lo, hi = max(0, i - half), min(len(rows), i + half + 1)
        mean = [sum(rows[k][a] for k in range(lo, hi)) / (hi - lo) for a in range(len(AXES))]
        out.append([rows[i][a] - mean[a] for a in range(len(AXES))])
    return out


def from_clips(clips: list[dict], window_s: float) -> list[list[float]]:
    rows: list[list[float]] = []
    for clip in clips:
        rows.extend(highpass(axes_from_clip(clip), window_s))      # 每段各自去漂移再接起來
    return rows


# ── 佔位：程序化合成 ──────────────────────────────────────────────────────────


def _ease(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


class _Wander:
    """每隔幾秒挑一個新目標、平滑地移過去（看一下旁邊、換一下重心）。"""

    def __init__(self, rng: random.Random, spread: float, every: tuple[float, float], blend: float):
        self.rng, self.spread, self.every, self.blend = rng, spread, every, blend
        self.start = self.target = 0.0
        self.t0 = 0.0
        self.next_at = rng.uniform(*every)

    def at(self, t: float) -> float:
        if t >= self.next_at:
            self.start = self.at_blend(t)
            self.target = self.rng.uniform(-self.spread, self.spread)
            self.t0 = t
            self.next_at = t + self.rng.uniform(*self.every)
        return self.at_blend(t)

    def at_blend(self, t: float) -> float:
        return self.start + (self.target - self.start) * _ease((t - self.t0) / self.blend)


def synthetic(seconds: float, seed: int) -> list[list[float]]:
    rng = random.Random(seed)
    weight = _Wander(rng, 2.2, (4.0, 9.0), 1.4)       # 換重心（身體左右歪）
    look = _Wander(rng, 9.0, (2.5, 6.5), 0.9)         # 左右看
    nod = _Wander(rng, 3.0, (3.0, 7.0), 1.1)          # 抬頭／低頭一點
    tilt = _Wander(rng, 3.5, (5.0, 10.0), 1.3)        # 歪頭
    breath_rate = 0.27
    rows = []
    for f in range(int(seconds * RATE)):
        t = f / RATE
        breath = math.sin(2 * math.pi * breath_rate * t + 0.4 * math.sin(0.11 * t))
        sway = math.sin(0.5 * t) * 0.6 + math.sin(0.23 * t + 1.3) * 0.5
        w = weight.at(t)
        rows.append([
            0.5 * breath + 0.3 * math.sin(0.17 * t),          # bodyX：呼吸帶一點前後
            0.4 * w + 0.8 * math.sin(0.09 * t),               # bodyY：上身微轉
            w + sway,                                         # bodyZ：重心＋左右晃
            0.9 * breath,                                     # chestX：呼吸
            nod.at(t) - 0.4 * breath,                         # headX
            look.at(t) - 0.3 * w,                             # headY：看旁邊（身體轉時頭會補回來一點）
            tilt.at(t) - 0.5 * w,                             # headZ：歪頭，重心換邊時頭反向扶正
        ])
    return rows


# ── Kimodo ───────────────────────────────────────────────────────────────────


def kimodo_generate(url: str, token: str, prompt: str, seconds: float, seed: int) -> dict:
    body = json.dumps({"prompt": prompt, "seconds": seconds, "seed": seed, "tag": "idle"}).encode()
    request = urllib.request.Request(url.rstrip("/") + "/v1/motion", data=body,
                                     headers={"Content-Type": "application/json",
                                              **({"X-Motion-Token": token} if token else {})})
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.loads(response.read().decode("utf-8"))


# ── 輸出 ─────────────────────────────────────────────────────────────────────


def write(rows: list[list[float]], source: str, out: pathlib.Path) -> None:
    if len(rows) < RATE * 10:
        raise SystemExit(f"只有 {len(rows)} 幀（< 10 秒），不夠擬合")
    payload = {
        "format": FORMAT,
        "sampleRateHz": RATE,
        "axes": list(AXES),
        "source": source,
        "frames": [[round(v, 3) for v in row] for row in rows],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    spans = [max(r[a] for r in rows) - min(r[a] for r in rows) for a in range(len(AXES))]
    print(f"✓ {out}（{len(rows)} 幀 = {len(rows) / RATE:.1f} 秒，{out.stat().st_size // 1024} KB）")
    print("  各軸峰峰值（度）：" + "  ".join(f"{name} {span:.1f}" for name, span in zip(AXES, spans)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    syn = sub.add_parser("synthetic", help="程序化合成的佔位資料")
    syn.add_argument("--seconds", type=float, default=90)
    syn.add_argument("--seed", type=int, default=7)
    syn.add_argument("--out", type=pathlib.Path, required=True)

    conv = sub.add_parser("from-clips", help="bamotion/1 片段 → 7 軸")
    conv.add_argument("clips", nargs="+", type=pathlib.Path)
    conv.add_argument("--highpass", type=float, default=8.0, help="去漂移的移動平均視窗（秒）")
    conv.add_argument("--out", type=pathlib.Path, required=True)

    kim = sub.add_parser("kimodo", help="叫 Kimodo 生成待機片段，再轉成 7 軸")
    kim.add_argument("--url", default="http://127.0.0.1:8790")
    kim.add_argument("--token", default="")
    kim.add_argument("--seconds", type=float, default=10)
    kim.add_argument("--seeds", type=int, default=2)
    kim.add_argument("--clips-dir", type=pathlib.Path, required=True, help="生成的片段存這裡（可續跑）")
    kim.add_argument("--highpass", type=float, default=8.0)
    kim.add_argument("--out", type=pathlib.Path, required=True)

    args = parser.parse_args()
    if args.command == "synthetic":
        write(synthetic(args.seconds, args.seed),
              f"synthetic placeholder (magic_idle.py synthetic --seconds {args.seconds:g} --seed {args.seed})", args.out)
        return 0

    if args.command == "from-clips":
        clips = [json.loads(path.read_text(encoding="utf-8")) for path in args.clips]
        write(from_clips(clips, args.highpass), f"bamotion/1 clips: {len(clips)}", args.out)
        return 0

    args.clips_dir.mkdir(parents=True, exist_ok=True)
    clips = []
    for p_index, prompt in enumerate(IDLE_PROMPTS):
        for seed in range(1, args.seeds + 1):
            path = args.clips_dir / f"idle-{p_index}-{seed}.json"
            if not path.exists():
                print(f"  生成 idle-{p_index}-{seed}：{prompt}", flush=True)
                clip = kimodo_generate(args.url, args.token, prompt, args.seconds, seed)
                path.write_text(json.dumps(clip, ensure_ascii=False), encoding="utf-8")
            clips.append(json.loads(path.read_text(encoding="utf-8")))
    write(from_clips(clips, args.highpass),
          f"Kimodo idle: {len(IDLE_PROMPTS)} prompts × {args.seeds} seeds × {args.seconds:g}s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
