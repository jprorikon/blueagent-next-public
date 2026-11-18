#!/usr/bin/env python3
"""量 Kimodo 的耗時拆解：文字編碼 vs 擴散步數（給「半即時」評估用）。

在 5070 那台的 kimodo-env 下跑：
    TEXT_ENCODER_DEVICE=cpu TEXT_ENCODERS_DIR=~/kimodo-text-encoders \
      ~/kimodo-env/bin/python probe_timing.py
"""
import os
import time

import torch

os.environ.setdefault("TEXT_ENCODER_DEVICE", "cpu")

from kimodo import load_model  # noqa: E402

PROMPT = "A girl happily waves her right hand to say hello."
FRAMES = 90  # 3 秒 × 30fps

print("── 載入模型 ──", flush=True)
t0 = time.time()
model, resolved = load_model("kimodo-soma-rp", device="cuda:0", default_family="Kimodo",
                             return_resolved_name=True)
print(f"載入 {resolved} {time.time() - t0:.1f}s", flush=True)
print("text_encoder device =", getattr(model.text_encoder, "_device", "?")
      if hasattr(model.text_encoder, "_device") else "?", flush=True)

print("\n── 文字編碼（第一次含暖機）──", flush=True)
t = time.time()
feat, length = model.text_encoder([PROMPT])
print(f"encode #1: {time.time() - t:.2f}s  feat={tuple(feat.shape)}", flush=True)
t = time.time()
feat, length = model.text_encoder([PROMPT])
print(f"encode #2: {time.time() - t:.2f}s", flush=True)

print("\n── 各步數的生成（用同一份 text_feat 快取）──", flush=True)
for steps in (100, 50, 30, 20, 15, 10):
    for _ in range(2):                    # 第一次含暖機，取第二次
        torch.cuda.synchronize()
        t = time.time()
        out = model(
            [PROMPT], [FRAMES],
            constraint_lst=[],
            num_denoising_steps=steps,
            num_samples=1,
            multi_prompt=True,
            post_processing=True,
            return_numpy=True,
        )
        torch.cuda.synchronize()
        dt = time.time() - t
    print(f"steps={steps:<3} 生成 {dt:.2f}s  (每步 {dt / steps * 1000:.0f}ms)  "
          f"pose={out['posed_joints'].shape}", flush=True)

print("\n── 含文字編碼的完整一次（steps=30，模擬即時生成）──", flush=True)
t = time.time()
out = model([PROMPT], [FRAMES], constraint_lst=[], num_denoising_steps=30, num_samples=1,
            multi_prompt=True, post_processing=True, return_numpy=True)
print(f"encode+generate(30) = {time.time() - t:.2f}s", flush=True)

free, total = torch.cuda.mem_get_info()
print(f"\n顯存：free {free / 2**30:.1f} GiB / total {total / 2**30:.1f} GiB", flush=True)
