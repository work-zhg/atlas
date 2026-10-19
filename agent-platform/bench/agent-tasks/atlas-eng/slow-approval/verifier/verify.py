import sys

try:
    got = open("out.txt", encoding="utf-8").read().strip()
except FileNotFoundError:
    sys.exit("没有 out.txt")
if got != "42":
    sys.exit(f"输出不对：{got!r}")
print("ok")
