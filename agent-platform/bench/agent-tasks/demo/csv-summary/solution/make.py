import csv, json

by = {}
for row in csv.DictReader(open("sales.csv", encoding="utf-8")):
    try:
        v = float(row["amount"])
    except ValueError:
        continue
    by[row["region"]] = by.get(row["region"], 0.0) + v
json.dump(
    {"by_region": {k: round(v, 1) for k, v in by.items()}, "total": round(sum(by.values()), 1)},
    open("summary.json", "w", encoding="utf-8"),
    ensure_ascii=False,
)
