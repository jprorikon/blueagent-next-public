# motion_service —— Kimodo 動作服務（GPU 機）

把文字變成一段 `bamotion/1` 動作片段。規格與契約在 [`../../docs/PLAN-motion-kimodo-20261031.md`](../../docs/PLAN-motion-kimodo-20261031.md)
§3（服務）／§4（片段格式）。**動作庫、快取、`motion` 幀、`/api/motion/<id>` 都在 blueagent-next 那一側**
（§5），這裡只有「生成」。

## 機器

Linux + NVIDIA（Ampere 以後）。全 GPU 約 17 GB 顯存；不夠就用 `TEXT_ENCODER_DEVICE=cpu`
（顯存 <3 GB，可與 ComfyUI 共存，代價是每段慢一點）。**NAS 與 Mac 都不行**。

現役部署：5070 那台（12 GB／29 GB RAM），走 CPU 文字編碼器。

## 安裝（一次性）

```bash
# 1) 建 venv（系統 Python 太新就讓 uv 自帶一個 3.12）
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv --python 3.12 --seed ~/kimodo-env
~/.local/bin/uv pip install --python ~/kimodo-env/bin/python \
    --index-url https://download.pytorch.org/whl/cu128 torch
~/.local/bin/uv pip install --python ~/kimodo-env/bin/python \
    "kimodo @ git+https://github.com/nv-tlabs/kimodo.git" fastapi uvicorn pydantic huggingface_hub

# 2) 模型
hf download nvidia/Kimodo-SOMA-RP-v1.2 --local-dir ~/models/kimodo-soma-rp-v1.2
hf download McGill-NLP/LLM2Vec-Meta-Llama-3-8B-Instruct-mntp           --local-dir ~/kimodo-text-encoders/McGill-NLP/LLM2Vec-Meta-Llama-3-8B-Instruct-mntp
hf download McGill-NLP/LLM2Vec-Meta-Llama-3-8B-Instruct-mntp-supervised --local-dir ~/kimodo-text-encoders/McGill-NLP/LLM2Vec-Meta-Llama-3-8B-Instruct-mntp-supervised
```

### 文字編碼器的底模為什麼要用鏡像

Kimodo 的文字編碼器是 **LLM2Vec**（`McGill-NLP/LLM2Vec-Meta-Llama-3-8B-Instruct-mntp`），
那兩個倉庫**沒有 gated**；但它的 `adapter_config.json` 裡 `base_model_name_or_path` 指向
**被 gated 的 `meta-llama/Meta-Llama-3-8B-Instruct`**（403）。解法：下一份**同樣權重、沒 gated** 的鏡像，
把兩個 adapter 的 base 指過去——

```bash
hf download NousResearch/Meta-Llama-3-8B-Instruct --local-dir ~/models/llama3-8b-instruct
python - <<'PY'
import json, pathlib
base = str(pathlib.Path.home() / "models" / "llama3-8b-instruct")
for name in ("LLM2Vec-Meta-Llama-3-8B-Instruct-mntp", "LLM2Vec-Meta-Llama-3-8B-Instruct-mntp-supervised"):
    p = pathlib.Path.home() / "kimodo-text-encoders" / "McGill-NLP" / name / "adapter_config.json"
    d = json.loads(p.read_text()); d["base_model_name_or_path"] = base; p.write_text(json.dumps(d, indent=2))
    print("patched", p)
PY
```

備選鏡像：`unsloth/llama-3-8b-Instruct`（同樣沒 gated）。

## 跑

```bash
cd ~/blueagent-next          # 這份 repo（服務要 import app/client/tools/motion_clip.py）
TEXT_ENCODER_DEVICE=cpu \
TEXT_ENCODERS_DIR=$HOME/kimodo-text-encoders \
MOTION_TOKEN=<共享 token> \
  ~/kimodo-env/bin/uvicorn motion_service.main:app --host 0.0.0.0 --port 8790
```

| env | 預設 | 說明 |
|---|---|---|
| `KIMODO_MODEL` | `kimodo-soma-rp` | 模型名（`load_model` 解析） |
| `KIMODO_DEVICE` | `cuda:0` | 生成裝置 |
| `Text_ENCODER_DEVICE` | — | 設 `cpu` 把文字編碼器放到記憶體（12 GB 卡必須） |
| `TEXT_ENCODERS_DIR` | — | 本地 adapter 目錄（見上） |
| `MOTION_TOKEN` | 空 | 共享 token；設了就要求 `X-Motion-Token` |
| `KIMODO_QUEUE_MAX` | `4` | 排隊上限，滿了回 503（呼叫方當「這輪沒動作」） |
| `KIMODO_STEPS` | `100` | 擴散步數（降了更快、質量自己量） |

只開在 tailnet／內網，**不暴露公網**（§2.3）。

## 驗

```bash
curl -s localhost:8790/v1/health
curl -s -X POST localhost:8790/v1/motion -H 'Content-Type: application/json' \
  -H "X-Motion-Token: $MOTION_TOKEN" \
  -d '{"prompt":"A girl happily waves her right hand to say hello.","seconds":4,"seed":1,"tag":"greet"}' \
  | python3 ../../app/client/tools/motion_clip.py validate /dev/stdin
```

服務自己的自檢：`check_tpose_relative()`（§3）——`global_rot_mats` 若不是「相對 T-pose 的世界旋轉」，
上臂方向會歪 >10°，服務直接回 500，**不會回半成品**。

## 生成動作庫

在 blueagent-next 這台跑（服務在 GPU 機上）：

```bash
python3 motion_service/build_library.py --url http://<gpu-host>:8790 --token <token> \
    --out data/motion --seeds 2
```

12 個 tag × 2 seed 寫進 `$BLUEAGENT_DATA_DIR/motion/`（`<id>.json` ＋ `index.json`），
每段都過 `motion_clip.py validate`；`MotionLibrary` 下次查詢時自動看到新檔（不用重啟）。
