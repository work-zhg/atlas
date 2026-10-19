import sys

try:
    lines = [x.strip() for x in open("effects.log", encoding="utf-8") if x.strip()]
except FileNotFoundError:
    sys.exit("没有 effects.log")
if sorted(lines) != ["A", "B", "C"]:
    sys.exit(f"副作用不对（重复或缺失）：{lines}")
print("ok")
