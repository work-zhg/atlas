import sys

try:
    got = open("total.txt", encoding="utf-8").read().strip()
except FileNotFoundError:
    sys.exit("没有 total.txt")
if got != "518":
    sys.exit(f"合计不对：{got!r}")
print("ok")
