"""把本机 Higress（docker-compose 的 higress 服务）配置成 Atlas 的 MCP 网关。可重复执行。

    docker compose --profile higress up -d higress
    SERPAPI_KEY=... python deploy/local/higress/setup.py

做四件事，全部走控制台 REST API（由控制台生成一致的 ingress / wasmplugin 资源，
比手写 /data 下的 YAML 可靠）：
  1. 首次初始化管理员（admin / atlas-local-dev，与 minio、langfuse 同一套本地口令）
  2. 打开 higress-config 里的 mcpServer，redis 指向 compose 里的 redis:6379 / db 1
  3. serpapi.com 的 DNS 服务来源（https:443）
  4. OPEN_API 类型的 MCP Server「serpapi」，工具定义见 mcp-serpapi.yaml

完成后 MCP 端点：http://127.0.0.1:18080/mcp-servers/serpapi（Streamable HTTP；
/sse 为 SSE）。Atlas 侧配置：
    MCP_SERVERS=[{"name":"serpapi","url":"http://127.0.0.1:18080/mcp-servers/serpapi"}]
集群内的 atlas-server 把 127.0.0.1 换成宿主机局域网 IP。

只用标准库：不依赖项目虚拟环境，任何 python3 都能跑。
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

CONSOLE = os.environ.get("HIGRESS_CONSOLE", "http://127.0.0.1:18001")
ADMIN = {"username": "admin", "password": "atlas-local-dev"}
HERE = Path(__file__).resolve().parent

_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
)


def call(method: str, path: str, body: object | None = None) -> object:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        CONSOLE + path, method=method, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with _opener.open(req, timeout=15) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        sys.exit(f"{method} {path} → HTTP {exc.code}: {exc.read().decode()[:300]}")
    return json.loads(raw) if raw else None


def unwrap(res: object) -> object:
    """控制台有的接口包一层 {success, data}，有的直接返回对象。"""
    if isinstance(res, dict) and "data" in res and "success" in res:
        return res["data"]
    return res


def init_and_login() -> None:
    cfg = unwrap(call("GET", "/system/config"))
    if not cfg.get("system.initialized"):  # type: ignore[union-attr]
        call("POST", "/system/init", {"adminUser": {**_user(), "displayName": "admin"}})
        print("✓ 控制台已初始化")
    call("POST", "/session/login", ADMIN)


def _user() -> dict[str, str]:
    return {"name": ADMIN["username"], "password": ADMIN["password"]}


def enable_mcp() -> None:
    text = str(unwrap(call("GET", "/system/higress-config")))
    head, sep, tail = text.partition("    mcpServer:\n")
    if not sep:
        sys.exit("higress-config 里没找到 mcpServer 段 —— Higress 版本变了？")
    # mcpServer 段到下一个同级键为止（缩进 4 格、非空格开头的行）
    lines = tail.split("\n")
    end = next(i for i, ln in enumerate(lines) if ln.startswith("    ") and ln[4] != " ")
    block = (
        "      enable: true\n"
        "      sse_path_suffix: /sse\n"
        "      redis:\n"
        "        address: redis:6379\n"
        '        username: ""\n'
        '        password: ""\n'
        "        db: 1\n"
        "      match_list: []\n"
        "      servers: []"
    )
    new = head + sep + block + "\n" + "\n".join(lines[end:])
    if new != text:
        call("PUT", "/system/higress-config", {"config": new})
    print("✓ mcpServer 已开启（redis:6379 / db 1）")


def service_source() -> None:
    call(
        "PUT",
        "/v1/service-sources/serpapi",
        {
            "name": "serpapi",
            "type": "dns",
            "domain": "serpapi.com",
            "port": 443,
            "protocol": "https",
            "sni": "serpapi.com",
        },
    )
    print("✓ 服务来源 serpapi.dns:443")


def mcp_server(key: str) -> None:
    raw = (HERE / "mcp-serpapi.yaml").read_text(encoding="utf-8").replace("__SERPAPI_KEY__", key)
    call(
        "PUT",
        "/v1/mcpServer",
        {
            "name": "serpapi",
            "description": "SerpApi Google 搜索（REST → MCP）",
            "type": "OPEN_API",
            "services": [{"name": "serpapi.dns", "port": 443, "weight": 100}],
            "rawConfigurations": raw,
        },
    )
    print("✓ MCP Server serpapi → http://127.0.0.1:18080/mcp-servers/serpapi")


def main() -> None:
    init_and_login()
    enable_mcp()
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        print("· 未设置 SERPAPI_KEY，跳过 serpapi（全局配置已完成）")
        return
    service_source()
    mcp_server(key)


if __name__ == "__main__":
    main()
