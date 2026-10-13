import csv, json, sys

want = {}
for row in csv.DictReader(open("/verifier/sales.csv", encoding="utf-8")):
    try:
        v = float(row["amount"])
    except ValueError:
        continue
    want[row["region"]] = want.get(row["region"], 0.0) + v
try:
    got = json.load(open("summary.json", encoding="utf-8"))
except Exception as exc:
    sys.exit(f"summary.json 读不了：{exc}")
by = got.get("by_region") or {}
bad = [k for k in want if abs(float(by.get(k, -1)) - round(want[k], 1)) > 0.051]
if bad or set(by) != set(want):
    sys.exit(f"by_region 不对：{bad or sorted(by)}")
if abs(float(got.get("total", -1)) - round(sum(want.values()), 1)) > 0.051:
    sys.exit(f"total 不对：{got.get('total')} vs {round(sum(want.values()), 1)}")
print("ok")
