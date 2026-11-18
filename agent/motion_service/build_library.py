"""預生成動作庫（`docs/PLAN-motion-kimodo-20261031.md` §5.1）：對 Kimodo 服務打 12 個 tag，
寫成 `$BLUEAGENT_DATA_DIR/motion/<id>.json` ＋ `index.json`。

用法（在 blueagent-next 這台跑，服務在 GPU 機上）：

    python3 motion_service/build_library.py --url http://127.0.0.1:8790 --token *** \\
        --out data/motion --seeds 2

- 每個 tag 生成 `--seeds` 段（不同 seed），播放時輪流挑，避免每次一模一樣
- 已存在的片段（同 id）不重打；中途中斷可以續跑
- 每段都過 `app/client/tools/motion_clip.py validate`，不合格不進庫
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys
import urllib.error
import urllib.request

#: §5.1 的初版 tag 與 prompt（英文；Kimodo 只訓練過英文）；要求原地動作，不走路
TAGS: tuple[tuple[str, str, float], ...] = (
    ("greet", "A girl happily waves her right hand to say hello.", 3),
    ("nod", "A person nods their head twice in agreement.", 2),
    ("shake", "A person shakes their head to say no.", 2),
    ("bow", "A girl bows politely and stands back up.", 3),
    ("laugh", "A girl laughs happily, shoulders shaking, hand near her mouth.", 3),
    ("shy", "A shy girl fidgets with her hands in front of her and sways slightly.", 4),
    ("think", "A person puts a hand on their chin and thinks.", 4),
    ("cheer", "A girl raises both arms and cheers excitedly.", 3),
    ("surprised", "A person is startled and leans back in surprise.", 2),
    ("shrug", "A person shrugs with both hands open.", 2),
    ("sad", "A person lowers their head sadly and slumps their shoulders.", 4),
    ("point", "A person points forward with their right hand while explaining.", 3),
)


def _validator():
    path = pathlib.Path(__file__).resolve().parents[2] / "app" / "client" / "tools" / "motion_clip.py"
    spec = importlib.util.spec_from_file_location("motion_clip", path)
    module = importlib.util.module_from_spec(spec)        # type: ignore[arg-type]
    spec.loader.exec_module(module)                       # type: ignore[union-attr]
    return module


def generate(url: str, token: str, prompt: str, seconds: float, seed: int, steps: int) -> dict:
    body = json.dumps({"prompt": prompt, "seconds": seconds, "seed": seed, "steps": steps}).encode()
    request = urllib.request.Request(url.rstrip("/") + "/v1/motion", data=body,
                                     headers={"Content-Type": "application/json",
                                              **({"X-Motion-Token": token} if token else {})})
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8790")
    parser.add_argument("--token", default="")
    parser.add_argument("--out", required=True, help="$BLUEAGENT_DATA_DIR/motion")
    parser.add_argument("--seeds", type=int, default=2)
    parser.add_argument("--steps", type=int, default=100)
    parser.add_argument("--only", default="", help="只跑這些 tag（逗號分隔）")
    args = parser.parse_args()

    clip_mod = _validator()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    wanted = {t.strip() for t in args.only.split(",") if t.strip()}
    index_path = out / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        index = {"tags": {}}
    tags = index.setdefault("tags", {})

    made = skipped = failed = 0
    for tag, prompt, seconds in TAGS:
        if wanted and tag not in wanted:
            continue
        for seed in range(1, args.seeds + 1):
            try:
                clip = generate(args.url, args.token, prompt, seconds, seed, args.steps)
            except urllib.error.HTTPError as exc:
                print(f"✗ {tag} seed={seed} HTTP {exc.code}: {exc.read()[:200]!r}", flush=True)
                failed += 1
                continue
            except Exception as exc:                       # noqa: BLE001
                print(f"✗ {tag} seed={seed} 失敗：{exc!r}", flush=True)
                failed += 1
                continue
            errors = clip_mod.validate(clip)
            if errors:
                print(f"✗ {tag} seed={seed} 片段不合格：{'; '.join(errors)}", flush=True)
                failed += 1
                continue
            clip["tag"] = tag
            clip["seed"] = seed
            path = out / f"{clip['id']}.json"
            if path.exists():
                skipped += 1
            else:
                path.write_text(json.dumps(clip, ensure_ascii=False), encoding="utf-8")
                made += 1
            ids = tags.setdefault(tag, [])
            if clip["id"] not in ids:
                ids.append(clip["id"])
            print(f"✓ {tag} seed={seed} {clip['seconds'] if 'seconds' in clip else clip['frames'] / clip['fps']:.1f}s "
                  f"→ {clip['id']} ({len(clip['joints'])} 關節)", flush=True)
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"── 完成：新增 {made}／已存在 {skipped}／失敗 {failed}；庫共 {sum(len(v) for v in tags.values())} 段 ──")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
