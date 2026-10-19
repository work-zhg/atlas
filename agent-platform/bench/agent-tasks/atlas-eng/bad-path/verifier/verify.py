import sys

want = ["部署清单", "回滚步骤", "监控告警", "容量规划"]  # alpha beta delta gamma
try:
    got = [x.strip() for x in open("titles.txt", encoding="utf-8") if x.strip()]
except FileNotFoundError:
    sys.exit("没有 titles.txt")
if got != want:
    sys.exit(f"标题不对：{got}")
print("ok")
