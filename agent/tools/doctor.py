#!/usr/bin/env python3
"""體檢器 CLI：python3 agent/tools/doctor.py --data data --entity mira

一層一項，逐項說「它在哪找證據」。**只看證據，不看結論**：
  · 讀不到／沒資料 ⇒ pending（看不到不等於沒有）
  · 某一層沒有任何檢查項 ⇒ error（結構洞）
  · 檢查自己拋錯 ⇒ pending ＋ 例外型別

退出碼：有 error ⇒ 1；只有 warn／pending ⇒ 0。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1]))

from agent.brain.runtime.doctor import LAYERS, run  # noqa: E402

_MARK = {"ok": "✓", "warn": "!", "error": "✗", "pending": "·"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="BlueAgent Next 體檢（觀察面）")
    parser.add_argument("--data", default=str(_HERE.parents[1] / "data"), help="資料根目錄")
    parser.add_argument("--entity", default="mira", help="角色 id")
    parser.add_argument("--layer", default="", help="只看某一層（例：L4-index）")
    parser.add_argument("--json", action="store_true", help="輸出 JSON")
    args = parser.parse_args(argv)

    report = run(args.data, args.entity, only=(args.layer or None))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    print(f"體檢 · {report['entity']} · {args.data}")
    current = ""
    for item in report["items"]:
        if item["layer"] != current:
            current = item["layer"]
            label = LAYERS.get(current) or ("結構洞（體檢器檢查自己）"
                                            if current == "S0-structure" else current)
            print(f"\n[{current}] {label}")
        print(f"  {_MARK.get(item['status'], '?')} {item['status']:7s} {item['check']:18s}"
              f" {item['detail']}")
        print(f"      ↳ 證據：{item['evidence']}")
    counts = report["counts"]
    print(f"\n合計 ok={counts['ok']} warn={counts['warn']} "
          f"error={counts['error']} pending={counts['pending']}  worst={report['worst']}")
    if counts["pending"]:
        print("※ pending ＝「看不到」，不是「沒事」——先確認那個路徑真的存在／可讀。")
    if counts["error"]:
        print("※ 有 error：先修 error，再看 warn 是不是設計內的狀態。")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
