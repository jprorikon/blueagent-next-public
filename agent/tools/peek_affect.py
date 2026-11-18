"""看她現在的情緒數值（用來標定 TRPG 匯報門檻）。"""
import json
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "/app/data/affect/mira/app/state.json"
data = json.load(open(path, encoding="utf-8"))
view = data.get("view") or {}
affect = view.get("affect") or {}
print("valence:", affect.get("valence"), " arousal:", affect.get("arousal"),
      " status:", affect.get("evidence_status"))
print("expression:", (view.get("expression") or {}).get("profile"))
episodes = data.get("episodes") or []
print("episodes:", len(episodes))
for row in episodes[-3:]:
    a = row.get("affect") or {}
    print("  ", row.get("kind"), "dv=", a.get("delta_valence"), "da=", a.get("delta_arousal"))
