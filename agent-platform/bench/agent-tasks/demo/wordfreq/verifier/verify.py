import collections, pathlib, re, subprocess, sys

stop = set(pathlib.Path("/verifier/stopwords.txt").read_text().split())
c = collections.Counter()
for p in pathlib.Path("/verifier/docs").glob("*.md"):
    c.update(w for w in re.findall(r"[a-z]+", p.read_text().lower()) if w not in stop)
want = [f"{w}\t{n}" for w, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[:10]]
score = 0.0
try:
    got = [l.rstrip("\n") for l in open("result.tsv") if l.strip()]
    if got == want:
        score += 0.6
    else:
        print(f"result.tsv 不对：{got[:4]} vs {want[:4]}")
except FileNotFoundError:
    print("没有 result.tsv")
if pathlib.Path("wordfreq.py").exists():
    score += 0.1
if pathlib.Path("test_wordfreq.py").exists():
    r = subprocess.run(
        [sys.executable, "-m", "unittest", "test_wordfreq"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if r.returncode == 0 and "Ran 0 tests" not in r.stderr:
        score += 0.3
    else:
        print("单元测试没通过：" + r.stderr[-500:])
else:
    print("没有 test_wordfreq.py")
print(f"SCORE={score:.2f}")
sys.exit(0 if score >= 0.999 else 1)
