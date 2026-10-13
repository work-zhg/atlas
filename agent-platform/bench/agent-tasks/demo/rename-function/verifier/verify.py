import pathlib, subprocess, sys

left = [
    str(p) for p in pathlib.Path(".").rglob("*.py") if "slugfy" in p.read_text(errors="replace")
]
if left:
    sys.exit(f"还有 slugfy：{left}")
sys.path.insert(0, ".")
import textkit  # noqa: E402

if textkit.slugify("Hello, World!") != "hello-world":
    sys.exit("slugify 行为变了")
r = subprocess.run(
    [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
    capture_output=True,
    text=True,
    timeout=60,
)
print(r.stderr[-1500:])
sys.exit(r.returncode)
