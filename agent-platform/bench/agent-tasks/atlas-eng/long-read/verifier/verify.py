import sys

try:
    got = open("answer.txt", encoding="utf-8").read().strip().replace("，", ",").replace(" ", "")
except FileNotFoundError:
    sys.exit("没有 answer.txt")
if got != "北极星-7,谢颖,7":
    sys.exit(f"答案不对：{got!r}（期望 北极星-7,谢颖,7）")
print("ok")
