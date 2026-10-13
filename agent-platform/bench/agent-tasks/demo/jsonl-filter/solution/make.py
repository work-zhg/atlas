import json

rows = [json.loads(l) for l in open("users.jsonl") if l.strip()]
ok = [
    u
    for u in rows
    if isinstance(u.get("age"), (int, float))
    and not isinstance(u.get("age"), bool)
    and u["age"] >= 18
    and u.get("active") is True
]
json.dump(
    [u["name"] for u in sorted(ok, key=lambda u: (-u["age"], u["id"]))], open("adults.json", "w")
)
