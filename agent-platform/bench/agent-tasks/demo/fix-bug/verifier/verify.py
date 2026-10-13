import pathlib, shutil, subprocess, sys

if (
    pathlib.Path("test_calc.py").read_text()
    != pathlib.Path("/verifier/test_calc.orig.py").read_text()
):
    sys.exit("test_calc.py 被改过了")
shutil.copy("/verifier/test_hidden.py", "test_hidden.py")
r = subprocess.run(
    [sys.executable, "-m", "unittest", "test_calc", "test_hidden"],
    capture_output=True,
    text=True,
    timeout=60,
)
print(r.stderr[-1500:])
sys.exit(r.returncode)
