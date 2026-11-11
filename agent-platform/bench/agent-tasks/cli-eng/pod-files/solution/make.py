import hashlib
import os

paths = ["a/b/c/deep.txt", "数据/说明.txt", "img/logo.png", "big.txt"]
with open("sums.txt", "w", encoding="utf-8") as f:
    for p in paths:
        f.write(hashlib.sha256(open(p, "rb").read()).hexdigest() + "  " + p + "\n")
data = os.urandom(65536)
open("gen.bin", "wb").write(data)
open("gen.sha256", "w").write(hashlib.sha256(data).hexdigest() + "  gen.bin\n")
