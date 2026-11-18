#!/usr/bin/env python3
"""收真機心跳（UDP）——真機沒有 logcat，這是唯一可靠的診斷通道。

用法：
    python3 blueagent-next/app/client/tools/device_beat.py [--port 8767] [--seconds 60] [--out /tmp/hermes_voice/beats.jsonl]

每一包是一行 JSON（`EngineHeartbeat.Build()` 的產出），原樣存檔並在畫面上印成一行摘要。
"""
import argparse
import json
import socket
import time
from datetime import datetime

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8767)
parser.add_argument("--seconds", type=float, default=60.0)
parser.add_argument("--out", default="/tmp/hermes_voice/beats.jsonl")
args = parser.parse_args()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("0.0.0.0", args.port))
sock.settimeout(1.0)
print(f"[beat] 聽 0.0.0.0:{args.port}，最多 {args.seconds:.0f} 秒 ⇒ {args.out}", flush=True)

deadline = time.time() + args.seconds
count = 0
with open(args.out, "w", encoding="utf-8") as fout:
    while time.time() < deadline:
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        line = data.decode("utf-8", "replace").strip()
        count += 1
        fout.write(line + "\n")
        fout.flush()
        try:
            beat = json.loads(line)
            print(
                "  {t:>7.2f}s frame={frame:<6} state={state:<9} params={params:<3} "
                "mouth={mouth:<6} hostMouth={hostMouth:<6} playing={playing} play={playSec:<5} "
                "level={level:<6} replay={replay} probe={probe} link={link} seen={framesSeen} "
                "touched={framesTouched} sent={sent} failed={failed}".format(**beat),
                flush=True,
            )
        except Exception:
            print("  " + line[:200], flush=True)

print(f"[beat] 收工：{count} 包 ⇒ {args.out}", flush=True)
