"""印最近幾輪對話（所有 thread 的 transcript 按時間排序），看「混亂」長什麼樣。"""
import datetime
import json
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "/app/data/threads.json"
limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
data = json.load(open(path, encoding="utf-8"))
threads = data.get("threads") if isinstance(data.get("threads"), dict) else data
rows = []
if isinstance(threads, dict):
    items = threads.items()
elif isinstance(threads, list):
    items = [(str(item.get("id")), item) for item in threads]
else:
    items = []
for key, value in items:
    if not isinstance(value, dict):
        continue
    label = "?"
    k = value.get("key") or {}
    if isinstance(k, dict):
        label = f"{k.get('scope')}/{k.get('scene')}/{k.get('purpose')}"
    for turn in value.get("transcript") or []:
        rows.append((float(turn.get("at") or 0), str(value.get("id") or key)[-4:], label,
                     turn.get("role"), str(turn.get("text") or "")))
rows.sort()
tz = datetime.timezone(datetime.timedelta(hours=9))
for at, tid, label, role, text in rows[-limit:]:
    stamp = datetime.datetime.fromtimestamp(at, tz).strftime("%H:%M:%S")
    print(stamp, tid, f"{label:22}", f"{role:9}", text[:90].replace("\n", " / "))
