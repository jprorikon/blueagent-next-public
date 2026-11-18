"""Kimodo 動作服務（GPU 機）——規格見 `docs/PLAN-motion-kimodo-20261031.md` §3。

**只做兩件事**：把文字變成一段 `bamotion/1` 動作片段、回答健康狀態。不碰 blueagent-next 的資料，
動作庫與快取在 blueagent-next 那一側（§5.1）。

跑法（GPU 機上）：

    TEXT_ENCODER_DEVICE=cpu TEXT_ENCODERS_DIR=~/kimodo-text-encoders MOTION_TOKEN=... \\
      uvicorn motion_service.main:app --host 0.0.0.0 --port 8790

為什麼文字編碼器預設放 CPU：5070 只有 12 GB，全 GPU 要 17 GB；放 CPU 後顯存 <3 GB，可與 ComfyUI 共存。
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import tempfile
import threading
import time

import numpy as np
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

MODEL_NAME = os.environ.get("KIMODO_MODEL", "kimodo-soma-rp")
DEVICE = os.environ.get("KIMODO_DEVICE", "cuda:0")
#: 規格 §2.4：單 worker 串行、排隊上限 4，滿了直接 503（呼叫方當「這輪沒動作」）
QUEUE_MAX = int(os.environ.get("KIMODO_QUEUE_MAX", "4"))
DEFAULT_STEPS = int(os.environ.get("KIMODO_STEPS", "100"))
MOTION_TOKEN = os.environ.get("MOTION_TOKEN", "")

app = FastAPI(title="Kimodo motion service", version="1.0")

_model = None
_resolved = ""
_lock = threading.Lock()          # 一次只跑一個生成（單 worker 串行）
_pending = 0
_pending_lock = threading.Lock()
_clip_mod = None


def _clip_module():
    """載入 `app/client/tools/motion_clip.py`（與前端判斷格式的**同一份**程式）。"""
    global _clip_mod
    if _clip_mod is None:
        path = pathlib.Path(os.environ.get("MOTION_CLIP_PY")
                            or pathlib.Path(__file__).resolve().parents[2] / "app" / "client" / "tools" / "motion_clip.py")
        spec = importlib.util.spec_from_file_location("motion_clip", path)
        module = importlib.util.module_from_spec(spec)     # type: ignore[arg-type]
        spec.loader.exec_module(module)                    # type: ignore[union-attr]
        _clip_mod = module
    return _clip_mod


def _skeleton_names(count: int) -> list[str]:
    """NPZ 的 J 個關節叫什麼：77 ⇒ BVH 的關節名、30 ⇒ SOMA 內部骨架（`joints.p`）。"""
    import kimodo

    root = pathlib.Path(kimodo.__file__).resolve().parent / "assets" / "skeletons"
    if count == 77:
        bvh = root / "somaskel77" / "somaskel77_standard_tpose.bvh"
        return _clip_module().bvh_joint_names(bvh)
    if count == 30:
        import torch

        joints = torch.load(root / "somaskel30" / "joints.p", map_location="cpu", weights_only=False)
        if isinstance(joints, dict):
            joints = list(joints.values())
        names = []
        for joint in joints:
            names.append(str(getattr(joint, "name", joint)))
        if names:
            return names
    raise RuntimeError(f"NPZ 有 {count} 個關節、認不出是哪套骨架（預期 30 或 77）⇒ 先查 Kimodo 版本")


def _to_clip(out: dict, meta: dict) -> dict:
    """模型輸出（numpy）→ `bamotion/1`。關節名與 T-pose 慣例在這裡驗，錯了就 500 不回半成品。"""
    clip_mod = _clip_module()
    single = {k: (v[0] if hasattr(v, "shape") and len(v.shape) > 1 else v) for k, v in out.items()}
    posed = np.asarray(single["posed_joints"])                 # [T, J, 3]
    mats = np.asarray(single["global_rot_mats"])               # [T, J, 3, 3]
    names = _skeleton_names(mats.shape[1])
    npz = {"global_rot_mats": mats, "posed_joints": posed}
    clip_mod.check_tpose_relative(npz, names)                  # §3 必做自檢
    index = {name: i for i, name in enumerate(names)}
    missing = [joint for joint in clip_mod.BODY_JOINTS if joint not in index]
    if missing:
        raise RuntimeError(f"骨架裡沒有這些身體關節：{missing}")
    hips = single.get("root_positions")
    if hips is None:
        hips = posed[:, index["Hips"]]
    rotations = [[clip_mod.q_from_matrix(mats[t, index[joint]].tolist())
                  for joint in clip_mod.BODY_JOINTS] for t in range(mats.shape[0])]
    meta = dict(meta)
    meta.setdefault("model", "Kimodo-SOMA-RP-v1.2")
    return clip_mod.encode(rotations, np.asarray(hips).tolist(), 30, meta)


@app.on_event("startup")
def _startup() -> None:
    global _model, _resolved
    from kimodo import load_model                          # 只有 GPU 機有這個套件

    started = time.time()
    _model, _resolved = load_model(MODEL_NAME, device=DEVICE, default_family="Kimodo",
                                   return_resolved_name=True)
    print(f"[kimodo] 模型載入完成 {_resolved}（{time.time() - started:.1f}s，device={DEVICE}）", flush=True)


def _check_token(token: str | None) -> None:
    if MOTION_TOKEN and token != MOTION_TOKEN:
        raise HTTPException(status_code=401, detail="bad motion token")


class MotionRequest(BaseModel):
    prompt: str = Field(min_length=1)
    seconds: float = 4.0
    seed: int | None = None
    steps: int = DEFAULT_STEPS
    tag: str = ""


@app.get("/v1/health")
def health() -> dict:
    with _pending_lock:
        pending = _pending
    return {"ok": _model is not None, "model": _resolved or MODEL_NAME, "device": DEVICE, "queue": pending}


@app.post("/v1/motion")
def motion(req: MotionRequest, x_motion_token: str | None = Header(default=None)) -> dict:
    _check_token(x_motion_token)
    seconds = max(1.0, min(10.0, float(req.seconds)))         # §3：夾到 [1, 10]
    frames = max(1, int(round(seconds * 30)))
    global _pending
    with _pending_lock:
        if _pending >= QUEUE_MAX:
            raise HTTPException(status_code=503, detail="queue full")
        _pending += 1
    try:
        with _lock:                                          # 串行：一塊卡同時只跑一個
            started = time.time()
            # ⚠️ 一定要在生成前固定種子：以前只把 seed 寫進 meta、沒有真的餵給 torch ⇒
            #    同一個 (tag, seed) 每次生出來的片段都不一樣，動作庫重跑會長出重複且無法重現
            if req.seed is not None:
                from kimodo.tools import seed_everything
                seed_everything(int(req.seed))
            out = _model(
                [req.prompt],
                [frames],
                constraint_lst=[],
                num_denoising_steps=int(max(10, min(200, req.steps))),
                num_samples=1,
                multi_prompt=True,
                post_processing=True,
                return_numpy=True,
            )
            generated = time.time() - started
        clip = _to_clip(out, {"tag": req.tag, "prompt": req.prompt,
                              "seed": req.seed or 0, "model": "Kimodo-SOMA-RP-v1.2"})
        errors = _clip_module().validate(clip)
        if errors:
            raise HTTPException(status_code=500, detail="bad clip: " + "; ".join(errors))
        print(f"[kimodo] {req.tag or '-'} {seconds}s seed={req.seed} 生成 {generated:.1f}s "
              f"片段 {clip['id']}（{len(clip['joints'])} 關節）", flush=True)
        return clip
    finally:
        with _pending_lock:
            _pending -= 1
