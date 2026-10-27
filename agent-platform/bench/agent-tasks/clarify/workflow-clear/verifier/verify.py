"""需求澄清（doc/ask-user-question-design.html §12）：产出有没有体现用户心里的需求要点。

模糊题的指令里没有这些要点，Agent 只有问了才知道（ask_user_question，
由模拟用户按 user.brief 作答）；
清晰题的指令里写明了。部分得分 = 体现了几个要点。
"""

import sys
from pathlib import Path

FILE = "prototype.html"
POINTS = ['工单', '转派', '催办']

path = Path(FILE)
if not path.is_file():
    sys.exit(f"没有 {FILE}")
html = path.read_text(encoding="utf-8", errors="replace")
missing = [p for p in POINTS if p not in html]
print(f"SCORE={(len(POINTS) - len(missing)) / len(POINTS):.2f}")
if missing:
    sys.exit(f"没有体现这些需求：{missing}")
