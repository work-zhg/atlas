import subprocess
import sys

try:
    first = open("total.py", encoding="utf-8").readline().strip()
except FileNotFoundError:
    sys.exit("没有 total.py")
if first != "# owner: data-team-lin":
    sys.exit(f"没遵守前一个会话里交代的要求，第一行是：{first!r}")
out = subprocess.run([sys.executable, "total.py"], capture_output=True, text=True).stdout.strip()
if out != "90":
    sys.exit(f"输出不对：{out!r}")
print("ok")
