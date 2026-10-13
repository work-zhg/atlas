import collections
import pathlib
import re
import sys


def count(docs: pathlib.Path, stop: set[str]) -> collections.Counter:
    c = collections.Counter()
    for p in docs.glob("*.md"):
        c.update(w for w in re.findall(r"[a-z]+", p.read_text().lower()) if w not in stop)
    return c


def top(c: collections.Counter, n: int = 10) -> list[tuple[str, int]]:
    return sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


if __name__ == "__main__":
    stop = set(pathlib.Path("stopwords.txt").read_text().split())
    rows = top(count(pathlib.Path("docs"), stop))
    pathlib.Path("result.tsv").write_text("".join(f"{w}\t{n}\n" for w, n in rows))
    sys.exit(0)
