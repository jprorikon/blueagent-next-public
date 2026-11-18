#!/usr/bin/env python3
"""重產動作庫：**生到合格為止**（每個 tag 收滿 N 段通過 QC 的片段）。

跟 `build_library.py` 的差別：那邊是「固定 seed 1..N 生一輪，好壞都收」；
這裡每一段都過 `library_qc.check_clip`（時長／主導部位／幅度），不合格就換下一個 seed，
所以庫裡不會再出現「幅度 2.7°（看不到）」或「surprised 由小腿主導」這種片段。

用法：
    python3 motion_service/rebuild_library_qc.py \
        --url http://192.168.50.32:8790 --token <token> --out data/motion --per-tag 5
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import build_library as base          # noqa: E402  （沿用同一份 TAGS 與請求／驗證）
import library_qc as qc               # noqa: E402

#: 提示詞改寫：一句話只做一件事，並明說手／身體要怎樣（太含糊 Kimodo 會自己加戲）
PROMPTS = {
    "greet": "A girl stands still and waves her right hand once to say hello. Her feet do not move.",
    "nod": "A person nods their head down and up once. The body and arms stay still.",
    "shake": "A person turns their head left and right twice to say no. The body stays still.",
    "bow": "A person bows from the waist and comes back up. The arms stay relaxed at the sides.",
    "laugh": "A girl laughs, her shoulders shaking gently, one hand near her mouth.",
    "shy": "A shy girl lowers her head slightly and presses her hands together in front of her.",
    "think": "A person tilts their head and touches their chin with one hand, thinking.",
    "cheer": "A girl raises both arms above her head and cheers.",
    "surprised": "A person flinches and leans back slightly in surprise, hands coming up a little.",
    "shrug": "A person lifts both shoulders in a shrug, palms turning up.",
    "sad": "A person lowers their head and lets their shoulders drop, standing still.",
    "point": "A person points forward with their right hand and holds it there.",
}

#: 每個 tag 最多試幾個 seed（避免某個 tag 一直生不出合格的就卡死）
MAX_SEEDS = 14


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8790")
    parser.add_argument("--token", default="")
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--per-tag", type=int, default=5)
    parser.add_argument("--steps", type=int, default=100)
    parser.add_argument("--seed-base", type=int, default=101)
    parser.add_argument("--tags", default="", help="只跑這幾個 tag（逗號分隔；空＝全部）")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    clip_mod = base._validator()
    wanted = {name.strip() for name in args.tags.split(",") if name.strip()}
    tags: dict[str, list[str]] = {}
    rejected: list[str] = []
    index_path = args.out / "index.json"
    if index_path.exists():                       # 續跑：沿用既有庫，不要蓋掉別的 tag
        tags = {tag: list(ids) for tag, ids in
                json.loads(index_path.read_text(encoding="utf-8")).get("tags", {}).items()}

    for tag, _prompt, seconds in base.TAGS:
        if wanted and tag not in wanted:
            continue
        prompt = PROMPTS.get(tag, _prompt)
        accepted = 0
        seed = args.seed_base
        attempts = 0
        while accepted < args.per_tag and attempts < MAX_SEEDS:
            attempts += 1
            seed += 1
            clip = base.generate(args.url, args.token, prompt, seconds, seed, args.steps)
            clip["tag"] = tag
            problems = qc.check_clip(clip)
            errors = clip_mod.validate(clip)
            if errors:
                problems.append("格式：" + "; ".join(errors))
            if problems:
                rejected.append(f"{tag} seed={seed}: " + " / ".join(problems))
                print(f"  ✗ {tag} seed={seed} 不合格：{' / '.join(problems)}", flush=True)
                continue
            (args.out / f"{clip['id']}.json").write_text(
                json.dumps(clip, ensure_ascii=False), encoding="utf-8")
            tags.setdefault(tag, []).append(clip["id"])
            accepted += 1
            info = qc.summary(clip)
            print(f"  ✓ {tag} seed={seed} → {clip['id']}（主導 {info['dominant']} "
                  f"{info['peak']}°）", flush=True)
        if accepted < args.per_tag:
            print(f"  ! {tag} 只收到 {accepted}/{args.per_tag} 段", flush=True)

    (args.out / "index.json").write_text(
        json.dumps({"tags": {tag: ids for tag, ids in sorted(tags.items())}},
                   ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    total = sum(len(ids) for ids in tags.values())
    target_tags = len(wanted) if wanted else len(base.TAGS)
    print(f"\n── 完成：{total} 段 / {len(tags)} tag；被退 {len(rejected)} 段 ──")
    for line in rejected:
        print("  ·", line)
    return 0 if all(len(tags.get(tag, [])) >= args.per_tag for tag in
                    ({t[0] for t in base.TAGS} if not wanted else wanted)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
