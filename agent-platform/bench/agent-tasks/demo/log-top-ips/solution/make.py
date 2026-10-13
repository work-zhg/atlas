import collections

c = collections.Counter()
for line in open("access.log"):
    p = line.split()
    if len(p) > 8 and not p[8].startswith("5"):
        c[p[0]] += 1
open("top_ips.txt", "w").write(
    "".join(f"{ip} {n}\n" for ip, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[:3])
)
