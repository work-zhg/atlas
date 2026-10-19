import sys

want = [str(p) for p in range(2, 100) if all(p % d for d in range(2, int(p**0.5) + 1))]
try:
    got = [x.strip() for x in open("primes.txt", encoding="utf-8") if x.strip()]
except FileNotFoundError:
    sys.exit("没有 primes.txt")
if got != want:
    sys.exit(f"质数不对：{got[:10]}…")
print("ok")
