import subprocess, sys

want = [
    "FizzBuzz" if i % 15 == 0 else "Fizz" if i % 3 == 0 else "Buzz" if i % 5 == 0 else str(i)
    for i in range(1, 31)
]
try:
    out = subprocess.run(
        [sys.executable, "fizzbuzz.py"], capture_output=True, text=True, timeout=20
    ).stdout
except FileNotFoundError:
    sys.exit("没有 fizzbuzz.py")
got = [l.strip() for l in out.strip().splitlines()]
if got != want:
    sys.exit(f"输出不对：{got[:8]}…")
print("ok")
