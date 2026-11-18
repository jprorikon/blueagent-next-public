"""向量（語意）嵌入：**本機跑，不依賴外部服務**（owner 2026-10-23「可以 b」）。

為什麼要自己一套：原本的語意層靠 Hindsight（`192.168.50.36:8897`），那台機現在不在線，
適配器又把錯誤吞掉 ⇒ 生產上的召回一直是**字面 bigram**。結果「ラノベ」「小説」「小说」
「novel」彼此都搜不到。

模型：`intfloat/multilingual-e5-large`（中／日／繁／英同一空間，1024 維）。
  · e5 的規矩：**查詢加 `query: `、文件加 `passage: `**（不加會掉一截）
  · 走 ONNX（fastembed），CPU 即可；模型放在 volume（不進 image）
  · 載不到模型 ⇒ `available()` 回 False，呼叫端自動退回字面檢索（**不擋對話**）
"""

from __future__ import annotations

import os
import threading

# 純數學、跟後端無關 ⇒ 住 runtime 的向量倉旁邊；這裡匯出給舊 import 用
from agent.brain.runtime.memory.vector_store import cosine  # noqa: F401

DEFAULT_MODEL = os.environ.get("BLUEAGENT_EMBED_MODEL") or "intfloat/multilingual-e5-large"
DEFAULT_DIR = os.environ.get("BLUEAGENT_EMBED_DIR") or "/models/fastembed"
#: 有這個 URL 就優先走 HTTP（llama.cpp `--embeddings` 的 bge-m3，實測 11 ms/段；
#: 本機 fastembed 是 1 s/段，慢兩個數量級）
DEFAULT_URL = os.environ.get("BLUEAGENT_EMBED_URL") or ""
#: 兩種 HTTP 風格：`openai`（/v1/embeddings）｜`jina`（Mac mini 的 jina-clip-v2：/embed/text）
DEFAULT_STYLE = (os.environ.get("BLUEAGENT_EMBED_STYLE") or "openai").strip().lower()


class Embedder:
    """`embed(texts, kind="passage"|"query") -> list[list[float]]`；失敗回空清單。"""

    def __init__(self, *, model: str = DEFAULT_MODEL, cache_dir: str = DEFAULT_DIR,
                 url: str = DEFAULT_URL, timeout_s: float = 30.0,
                 style: str = DEFAULT_STYLE) -> None:
        #: `openai`（llama.cpp / 任何 OpenAI 相容）或 `jina`（Mac mini clip-serve）
        self.style = str(style or "openai").lower()
        #: HTTP embedding 端點（空＝走本機 fastembed）
        self.url = str(url or "").rstrip("/")
        self.timeout_s = float(timeout_s)
        self.model_name = str(model)
        self.cache_dir = str(cache_dir)
        self._model = None
        self._lock = threading.RLock()
        self.dim = 0
        self.last_error = ""
        self.calls = 0
        self.texts = 0

    def available(self) -> bool:
        if self.url:
            return bool(self.embed(["確認"], kind="query"))
        return self._ensure() is not None

    # ── HTTP（llama.cpp / OpenAI 相容 `/v1/embeddings`）────────────────────────
    def _embed_http(self, rows: list[str], prefix: str) -> list[list[float]]:
        import json
        import urllib.request
        if self.style == "jina":
            # Mac mini `~/.clip-serve`：POST /embed/text {"texts": [...]} → {"embeddings": [...]}
            body = json.dumps({"texts": rows}, ensure_ascii=False).encode("utf-8")
            endpoint = f"{self.url}/embed/text"
        else:
            body = json.dumps({"input": [prefix + row for row in rows],
                               "model": self.model_name}, ensure_ascii=False).encode("utf-8")
            endpoint = f"{self.url}/v1/embeddings"
        request = urllib.request.Request(
            endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        if self.style == "jina":
            vectors = [list(map(float, vec)) for vec in (data.get("embeddings") or [])]
        else:
            items = data.get("data") if isinstance(data, dict) else None
            if not items:
                raise ValueError("empty_embeddings")
            vectors = [list(map(float, item.get("embedding") or [])) for item in items]
        if vectors and not self.dim:
            self.dim = len(vectors[0])
        return vectors

    def _ensure(self):
        with self._lock:
            if self._model is not None:
                return self._model
            try:
                from fastembed import TextEmbedding          # 可選依賴
            except Exception as exc:                          # noqa: BLE001
                self.last_error = f"fastembed 不可用：{type(exc).__name__}"
                return None
            try:
                self._model = TextEmbedding(model_name=self.model_name,
                                            cache_dir=self.cache_dir)
                probe = list(self._model.embed(["passage: 次元確認"]))
                self.dim = len(probe[0]) if probe else 0
            except Exception as exc:                          # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._model = None
                return None
            return self._model

    def embed(self, texts, *, kind: str = "passage") -> list[list[float]]:
        rows = [str(text or "").strip() for text in (texts or ())]
        rows = [row for row in rows if row]
        if not rows:
            return []
        with self._lock:
            self.calls += 1
            self.texts += len(rows)
        # HTTP 優先：**不要先叫 fastembed**（它認不得 bge-m3，會直接報 unsupported）
        if self.url:
            try:
                return self._embed_http(rows, "")
            except Exception as exc:                          # noqa: BLE001
                # 遠端掛了就退回本機模型（慢但不會讓檢索整條死掉）
                self.last_error = f"http: {type(exc).__name__}: {exc}"
        model = self._ensure()
        if model is None:
            return []
        prefix = "query: " if str(kind).lower().startswith("query") else "passage: "
        try:
            return [list(map(float, vec)) for vec in model.embed([prefix + row for row in rows])]
        except Exception as exc:                              # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []

    def snapshot(self) -> dict:
        return {"model": self.model_name, "dir": self.cache_dir, "dim": self.dim,
                "available": self._model is not None, "calls": self.calls,
                "texts": self.texts, "error": self.last_error}
