import sys

try:
    got = open("count.txt", encoding="utf-8").read().strip()
except FileNotFoundError:
    sys.exit("没有 count.txt（被拒后没有换办法完成）")
if got != "137":
    sys.exit(f"行数不对：{got!r}")
print("ok")
