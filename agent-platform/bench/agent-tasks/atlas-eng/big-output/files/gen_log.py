"""生成约 1.3MB 的服务日志 app.log（确定性，可重复）。"""

import random

rng = random.Random(4711)
CODES = ["E1001", "E2002", "E3003", "E5005"]
with open("app.log", "w", encoding="utf-8") as f:
    for i in range(20000):
        ts = f"2026-10-13T{8 + i // 3600 % 10:02d}:{i // 60 % 60:02d}:{i % 60:02d}"
        rid = f"req-{rng.getrandbits(32):08x}"
        if i == 10_123:
            f.write(f"{ts} ERROR code=E4711 request_id=req-7f3a9c21 msg=payment ledger mismatch\n")
        elif rng.random() < 0.02:
            f.write(f"{ts} ERROR code={rng.choice(CODES)} request_id={rid} msg=upstream failed\n")
        else:
            f.write(f"{ts} INFO request_id={rid} path=/api/v1/items status=200 cost_ms={rng.randint(3, 900)}\n")
