import json, sys

rows = [json.loads(l) for l in open("/verifier/users.jsonl") if l.strip()]
ok = [
    u
    for u in rows
    if isinstance(u.get("age"), (int, float))
    and not isinstance(u.get("age"), bool)
    and u["age"] >= 18
    and u.get("active") is True
]
want = [u["name"] for u in sorted(ok, key=lambda u: (-u["age"], u["id"]))]
try:
    got = json.load(open("adults.json"))
except Exception as exc:
    sys.exit(f"adults.json 读不了：{exc}")
if got != want:
    sys.exit(f"结果不对：{got} vs {want}")
print("ok")
