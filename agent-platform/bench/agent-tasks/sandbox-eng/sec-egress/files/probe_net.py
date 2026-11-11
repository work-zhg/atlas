"""出站探测（bench/doc/sandbox-eng-cases.html SEC-02）：逐个尝试连接，写出连得上 / 连不上。

★ 目标按被测部署写（这里是 112：单节点 192.168.0.112，Service 网段 10.96.0.0/12）。换部署要改。
"""

import json
import socket

#: 必须连不上：集群网段、K8s API（平台的出站底线）
MUST_BLOCK = {
    "k8s-api:10.96.0.1:443": ("10.96.0.1", 443),
    "k8s-api-dns:443": ("kubernetes.default.svc.cluster.local", 443),
    "svc:atlas-server:8000": ("atlas-server.atlas-system.svc.cluster.local", 8000),
    "svc:postgres:5432": ("postgres.atlas-infra.svc.cluster.local", 5432),
}
#: 不判定，只记录：公网按策略放行；节点、局域网按部署的取舍放开（112 上放开）
RECORDED = {
    "public:example.com:80": ("example.com", 80),
    "node:192.168.0.112:22": ("192.168.0.112", 22),
    "node:192.168.0.112:6443": ("192.168.0.112", 6443),
    "lan:192.168.0.1:80": ("192.168.0.1", 80),
}


def probe(targets: dict[str, tuple[str, int]]) -> dict[str, str]:
    out = {}
    for name, (host, port) in targets.items():
        try:
            with socket.create_connection((host, port), timeout=3):
                out[name] = "open"
        except OSError as exc:
            out[name] = f"blocked: {type(exc).__name__}"
    return out


print(
    json.dumps(
        {"must_block": probe(MUST_BLOCK), "recorded": probe(RECORDED)},
        ensure_ascii=False,
        indent=2,
    )
)
