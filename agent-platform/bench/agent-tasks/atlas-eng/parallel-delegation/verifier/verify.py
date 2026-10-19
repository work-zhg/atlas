import json
import sys

want = json.load(open("/verifier/expected.json"))
try:
    got = json.load(open("sums.json", encoding="utf-8"))
except (FileNotFoundError, ValueError) as e:
    sys.exit(f"sums.json 不可读：{e}")
bad = {k: (got.get(k), v) for k, v in want.items() if got.get(k) != v}
if bad:
    sys.exit(f"和不对（得到, 期望）：{bad}")
print("ok")
