#!/usr/bin/env python3
"""把**真橋**抓下來的幀序列（`capture_bridge_sse.py` 的 `.frames.jsonl`）變成真機回放用的 fixture
（`BlueAgentUnity/Assets/Resources/engine_replay.json`）。

為什麼要有這一支（2026-10-12 真機踩到的兩個坑，一次講清楚）：

1. **形狀名的大小寫**：客戶端的形狀表是小寫（`closed/a/i/u/e/o`，與網頁版一致，見
   `shared/BlueAgent.Logic/Engine/MouthScript.cs`），而手寫的 fixture 用了大寫（`A`／`I`／`U`）
   ⇒ 全部落在「認不得 ⇒ closed」那條安全路上。症狀極具誤導性：**狀態機照樣 LISTENING→…→SPEAKING、
   聲音照樣放，只有嘴全程不動**（心跳裡 `mouth` 一直是 0，看起來像嘴的驅動壞了）。
2. **音訊**：手寫的 fixture 放的是一個正弦占位音（不是她的人聲）；真橋的 `speech` 幀帶的是
   **真的 TTS WAV**（32 kHz 單聲道）⇒ fixture 從真橋抓，這一格自然就是真的。

所以：**fixture 一律從真橋抓、不手寫**（與「自造 fixture 要用線上的真實形狀」同一條紀律）。
這一支順手當**閘**：形狀名不在表裡、`speech` 沒音訊／沒腳本、缺 `started`／`done` ⇒ 當場非零退出。

用法：
  python3 blueagent-next/app/client/tools/make_replay_fixture.py <frames.jsonl> [--out <engine_replay.json>] [--lead 0.4]
預設 out＝`BlueAgentUnity/Assets/Resources/engine_replay.json`（改完要 `deploy_unity.sh` ＋重建 APK）。
"""
import argparse
import base64
import io
import json
import os
import pathlib
import re
import struct
import sys
import wave

HERE = pathlib.Path(__file__).resolve().parent
UNITY_REPO = HERE.parent
MOUTH_SCRIPT = UNITY_REPO / "shared/BlueAgent.Logic/Engine/MouthScript.cs"
DEFAULT_OUT = UNITY_REPO / "BlueAgentUnity/Assets/Resources/engine_replay.json"

#: 必要的幀（缺了就當場炸：這一份 fixture 的用途就是驗這條路）
REQUIRED = ("started", "done")


def known_shapes() -> list[str]:
    """從共用邏輯層的 `MouthScript.MouthShapes` 讀出「認得的形狀」（機器真值，不手抄）。"""
    text = MOUTH_SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"MouthShapes\s*=\s*new\[\]\s*\{([^}]*)\}", text)
    if not m:
        raise SystemExit(f"讀不到 MouthShapes：{MOUTH_SCRIPT}")
    return [s.strip().strip('"') for s in m.group(1).split(",") if s.strip()]


def wav_summary(b64: str) -> tuple[float, int]:
    """回 `(秒數, 峰值)`——峰值是為了確認「這不是一段靜音」。"""
    raw = base64.b64decode(b64)
    with wave.open(io.BytesIO(raw)) as w:
        frames = w.readframes(w.getnframes())
        seconds = w.getnframes() / float(w.getframerate())
    count = len(frames) // 2
    peak = max((abs(v) for v in struct.unpack("<%dh" % count, frames[:count * 2])), default=0)
    return round(seconds, 2), peak


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("frames", help="capture_bridge_sse.py 產出的 *.frames.jsonl")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--lead", type=float, default=0.4,
                    help="第一幀在回放開始後幾秒落地（預設 0.4，與舊 fixture 同）")
    ap.add_argument("--note", default="", help="寫進 fixture 的說明（預設自動寫來源檔名）")
    args = ap.parse_args()

    rows = []
    for line in pathlib.Path(args.frames).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    if not rows:
        raise SystemExit("frames.jsonl 是空的")

    kinds = [r["event"] for r in rows]
    for need in REQUIRED:
        if need not in kinds:
            raise SystemExit(f"缺 `{need}` 幀（{kinds}）⇒ 這份擷取不完整，別拿來當 fixture")

    shapes = known_shapes()
    t0 = rows[0]["t"]
    steps = []
    problems = []
    speech_count = 0
    # ⚠️ **時基重建**（只有 `speech` 需要）：橋是一口氣把多句推出來的（實測 9 句的到達時間
    #    全都落在同 0.07 秒內），而客戶端是「收到就播」⇒ 照原時戳回放會**每一句都打斷前一句**
    #    （只有最後一句聽得到）。所以這裡把連續的 `speech` 按**自己的音長**排開
    #    （第 n 句 = 前一句結束那一刻），其餘幀（`started`／`delta`／`done`）照原本的到達時間，
    #    但不會早於前一句結束（`done` 本來就在最後一句之後）。這是**時間軸的重建、不是內容的發明**
    #    ——每一幀的 `data:` 仍是線上原樣。
    cursor = None                                  # 上一句的結束時刻（相對回放開始）
    for r in rows:
        data = r["data"]
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            obj = {}
        at = round(r["t"] - t0 + args.lead, 3)
        if r["event"] == "speech":
            speech_count += 1
            audio = obj.get("audio") or ""
            motion = obj.get("motion") or []
            if not audio:
                problems.append(f"speech#{speech_count} 沒有 audio（真機回放會沒聲音）")
            else:
                seconds, peak = wav_summary(audio)
                if peak == 0:
                    problems.append(f"speech#{speech_count} 的音訊是靜音（峰值 0）")
                print(f"  speech#{speech_count}: {seconds}s／峰值 {peak}／腳本 {len(motion)} 幀")
                at = round(max(at, cursor) if cursor is not None else at, 3)
                cursor = at + seconds
            if not motion:
                problems.append(f"speech#{speech_count} 沒有 motion（嘴型腳本）⇒ 嘴不會動")
            for f in motion:
                shape = f[1] if isinstance(f, (list, tuple)) and len(f) >= 2 else None
                if shape is None:
                    problems.append(f"speech#{speech_count} 的腳本有一列不是 [ms, shape]：{f!r}")
                elif shape not in shapes:
                    problems.append(f"speech#{speech_count} 的形狀 {shape!r} 不在形狀表 {shapes}"
                                    f"（大小寫要一致；線上送的是小寫）")
        elif cursor is not None and at < cursor:
            at = round(cursor, 3)                      # 收尾那些幀不該早於最後一句結束
        if r["event"] == "delta":
            if "emotion" in obj:
                print(f"  delta: 帶情緒 {obj.get('emotion')}／{obj.get('variant')}／{obj.get('intensity')}")
            else:
                print("  delta: ⚠️ 沒帶情緒（引擎會中性演；橋側 A 案之後應該要有）")
        steps.append({"at": at, "event": r["event"], "data": data})

    if speech_count == 0:
        problems.append("整份擷取裡沒有 speech 幀（沒有聲音也沒有嘴）")
    if problems:
        print("\n✗ fixture 不合格：", file=sys.stderr)
        for p in problems:
            print("   -", p, file=sys.stderr)
        return 2

    note = args.note or f"引擎回放用的橋幀序列（來源＝{os.path.basename(args.frames)}，真橋抓的；形狀與時序照線上）"
    out = {"note": note, "steps": steps}
    pathlib.Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n✓ 寫出 {args.out}：{len(steps)} 幀／{speech_count} 句（{os.path.getsize(args.out)} bytes）")
    print("  ⚠️ 改完要 `bash tools/deploy_unity.sh` ＋重建 APK 才上得了真機。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
