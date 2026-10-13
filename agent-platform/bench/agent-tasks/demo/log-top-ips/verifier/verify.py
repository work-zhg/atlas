import collections, sys

c = collections.Counter()
for line in open("/verifier/access.log"):
    parts = line.split()
    if len(parts) > 8 and not parts[8].startswith("5"):
        c[parts[0]] += 1
want = [f"{ip} {n}" for ip, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
try:
    got = [" ".join(l.split()) for l in open("top_ips.txt") if l.strip()]
except FileNotFoundError:
    sys.exit("没有 top_ips.txt")
if got != want:
    sys.exit(f"结果不对：{got} vs {want}")
print("ok")
