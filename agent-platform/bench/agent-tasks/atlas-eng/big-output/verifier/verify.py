import sys

try:
    got = open("answer.txt", encoding="utf-8").read().strip()
except FileNotFoundError:
    sys.exit("没有 answer.txt")
if got != "req-7f3a9c21":
    sys.exit(f"request_id 不对：{got!r}")
print("ok")
