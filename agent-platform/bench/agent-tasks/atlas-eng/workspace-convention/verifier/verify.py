import subprocess, sys
try:
    first = open("hello.py", encoding="utf-8").readline().strip()
except FileNotFoundError:
    sys.exit("没有 hello.py")
if first != "# ATLAS-CONVENTION":
    sys.exit(f"没遵守 ATLAS.md 的约定，第一行是：{first!r}")
out = subprocess.run([sys.executable, "hello.py"], capture_output=True, text=True).stdout
if out.strip() != "hello":
    sys.exit(f"输出不对：{out!r}")
print("ok")
