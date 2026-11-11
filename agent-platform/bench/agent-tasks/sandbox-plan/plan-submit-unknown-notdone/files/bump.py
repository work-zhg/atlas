import pathlib

p = pathlib.Path("version.txt")
major, minor, patch = p.read_text().strip().split(".")
p.write_text(f"{major}.{minor}.{int(patch) + 1}\n")
print("bumped to", p.read_text().strip())
