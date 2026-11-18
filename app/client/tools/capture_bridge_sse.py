#!/usr/bin/env python3
"""把橋（本機 :8900）真實吐出來的 SSE 位元組原樣抓下來，當作 C# 側 SSE 解析器的**線上形狀**真值。

為什麼要這一支：2026-10-12 的教訓——「自造 fixture 要用線上的真實形狀」。
嘴型腳本那次就是我照自己以為的形狀寫測試，兩邊一致地錯、測試一路綠。
SSE 這條同構：解析器要吃的就是這串位元組，所以抓真的。

輸出三個檔（同一組前綴）：
  *.txt          原始位元組（原樣，含 `event:`／`data:` 行的每一個字元）
  *.chunks.txt   每個網路 chunk 的到達時間與大小（診斷用）
  *.frames.jsonl **逐幀的到達時間**＋event 名＋`data:` 的內容原樣（給 `make_replay_fixture.py` 用；
                 真機回放要「照線上的形狀**與時序**」，時序就是這一份）

用法：
  python3 tools/capture_bridge_sse.py <out_dir> [--tts 1] [--text "..."] [--session foo]
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "..", "..", "docker", "blueagent-server", ".env")


def env_value(key: str) -> str:
    with open(ENV_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    raise SystemExit(f".env 裡沒有 {key}")


def main() -> int:
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    tts = "0"
    text = "ユニティ側の疎通確認です。一言だけ返して。"
    session = "hermes-unity-spike"
    args = sys.argv[2:]
    for i, a in enumerate(args):
        if a == "--tts":
            tts = args[i + 1]
        elif a == "--text":
            text = args[i + 1]
        elif a == "--session":
            session = args[i + 1]

    token = env_value("STREAM_TOKEN")
    payload = json.dumps({"text": text, "session": session, "soul": "aster",
                          "mode": "chat", "tts": int(tts)},
                         ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:8900/chat", data=payload, method="POST",
        headers={"Authorization": "Bearer " + token,
                 "X-Device": "unity-spike",
                 "Accept": "text/event-stream",
                 "Content-Type": "application/json; charset=utf-8"})

    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    raw_path = os.path.join(out_dir, f"sse_live_{stamp}_tts{tts}.txt")
    log_path = os.path.join(out_dir, f"sse_live_{stamp}_tts{tts}.chunks.txt")
    frame_path = os.path.join(out_dir, f"sse_live_{stamp}_tts{tts}.frames.jsonl")

    chunks = []
    total = 0
    frames = 0
    t0 = time.time()
    buf = ""                                   # 還沒湊成完整一幀的尾巴
    with urllib.request.urlopen(req, timeout=180) as resp:
        print(f"HTTP {resp.status} {resp.headers.get('Content-Type')}")
        with open(raw_path, "wb") as raw, open(log_path, "w", encoding="utf-8") as log, \
                open(frame_path, "w", encoding="utf-8") as fout:
            while True:
                block = resp.read1(4096) if hasattr(resp, "read1") else resp.read(4096)
                if not block:
                    break
                now = time.time() - t0
                total += len(block)
                chunks.append((now, len(block)))
                log.write(f"{now:8.3f}s  {len(block):5d} bytes\n")
                raw.write(block)
                raw.flush()

                # 逐幀切出來（以空行分幀，與 SseStream 同一條規則的粗版；只用來記錄，不當真值）。
                buf += block.decode("utf-8", "replace")
                while "\n\n" in buf:
                    head, buf = buf.split("\n\n", 1)
                    event = ""
                    data = ""
                    for line in head.splitlines():
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:"):
                            data = line[5:]            # ⚠️ 不裁：橋送的是 `data: {json}`（冒號後一個空白）
                    if not event:
                        continue
                    fout.write(json.dumps({"t": round(now, 3), "event": event, "data": data},
                                          ensure_ascii=False) + "\n")
                    fout.flush()
                    frames += 1
    elapsed = time.time() - t0
    print(f"bytes={total} chunks={len(chunks)} frames={frames} secs={elapsed:.2f}")
    print("raw   :", os.path.abspath(raw_path))
    print("chunks:", os.path.abspath(log_path))
    print("frames:", os.path.abspath(frame_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
