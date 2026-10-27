"""把本机 Higress（docker-compose 的 higress 服务）配置成 MCP 网关的上游。可重复执行。

    docker compose --profile higress up -d higress
    SERPAPI_KEY=... MCP_GATEWAY_CREDENTIAL=... python deploy/local/higress/setup.py

全部走控制台 REST API（由控制台生成一致的 ingress / wasmplugin 资源，比手写 /data 下的 YAML 可靠）：
  1. 首次初始化管理员（admin / atlas-local-dev，与 minio、langfuse 同一套本地口令）
  2. 打开 higress-config 里的 mcpServer，redis 指向 compose 里的 redis:6379 / db 1
  3. 只认 MCP 网关（doc/mcp-design.html §8.3，给了 MCP_GATEWAY_CREDENTIAL 时）：
     消费者 mcp-gateway 持有网关凭据；每个 MCP 服务只允许它调用；代理型（DIRECT_ROUTE）服务
     另挂一个插件，认证之后去掉凭据再转发（Higress 默认会把 Authorization 原样转给后端）
  4. serpapi.com 的 DNS 服务来源（https:443）
  5. OPEN_API 类型的 MCP Server「serpapi」，工具定义见 mcp-serpapi.yaml

完成后 MCP 端点：http://127.0.0.1:18080/mcp-servers/serpapi（Streamable HTTP）。它只接受
MCP 网关的请求：Atlas 和 Pod 连 MCP 网关，由网关登记（python -m atlas_mcpgateway service add serpapi …）。

★ 轮换网关凭据不停机：先 MCP_GATEWAY_CREDENTIAL=新 MCP_GATEWAY_CREDENTIAL_PREVIOUS=旧 跑一次
  （两把都认），MCP 网关切到新的之后，去掉 PREVIOUS 再跑一次。
★ 不给 MCP_GATEWAY_CREDENTIAL 时服务不开认证：谁都能直连，MCP 网关的接入检查会拒绝登记它。
★ 在控制台上新声明代理型（DIRECT_ROUTE）服务之后，认证选「只允许 mcp-gateway」，再跑一次
    python deploy/local/higress/setup.py --strip-credential <服务名>
  否则网关凭据会被转给这个服务的后端（它拿到就能绕过 MCP 网关）。

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


def call(method: str, path: str, body: object | None = None, *, missing_ok: bool = False) -> object:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        CONSOLE + path, method=method, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with _opener.open(req, timeout=15) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        if missing_ok and exc.code == 404:
            return None
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


# ───────────────────────────── 只认 MCP 网关（§8.3）

GATEWAY_CONSUMER = "mcp-gateway"
#: 认证之后去掉 Authorization 的插件：用内置 transformer 的镜像，但放在 AUTHZ 阶段。
#: ★ 内置 transformer 在 AUTHN 阶段、优先级比 key-auth 高，会在认证之前就把凭据删掉（全部 401）；
#:   MCP 服务的路由是 internal 的，控制台不许改它的 headerControl。2026-10-27 本机实测
STRIP_PLUGIN = "mcp-gateway-credential-strip"


def unwrap_or_none(res: object) -> object:
    return None if res is None else unwrap(res)


def gateway_consumer(credential: str, previous: str | None) -> None:
    values = [credential, *([previous] if previous and previous != credential else [])]
    body = {
        "name": GATEWAY_CONSUMER,
        "credentials": [{"type": "key-auth", "source": "BEARER", "values": values}],
    }
    if unwrap_or_none(call("GET", f"/v1/consumers/{GATEWAY_CONSUMER}", missing_ok=True)) is None:
        call("POST", "/v1/consumers", body)
    else:
        call("PUT", f"/v1/consumers/{GATEWAY_CONSUMER}", body)
    print(f"✓ 消费者 {GATEWAY_CONSUMER}（{len(values)} 把网关凭据）")


def gateway_auth() -> dict[str, object]:
    """MCP 服务的认证：只允许 MCP 网关这一个消费者。"""
    return {"type": "key-auth", "enable": True, "allowedConsumers": [GATEWAY_CONSUMER]}


def strip_credential(server: str) -> None:
    """代理型（DIRECT_ROUTE）服务：认证之后去掉网关凭据，后端拿不到它（§8.3、V4）。

    ★ REST 转 MCP（OPEN_API）不需要：Higress 按模板重新拼请求，不带 Authorization（实测）。
    """
    plugin = {
        "name": STRIP_PLUGIN,
        "title": "去掉 MCP 网关凭据",
        "description": "key-auth 认过之后去掉 Authorization，不转给后端",
        "imageRepository": "http://localhost:8002/plugins/transformer/2.0.1/plugin.wasm",
        "phase": "AUTHZ",
        "priority": 100,
        "category": "transform",
    }
    if unwrap_or_none(call("GET", f"/v1/wasm-plugins/{STRIP_PLUGIN}", missing_ok=True)) is None:
        call("POST", "/v1/wasm-plugins", plugin)
    route = f"mcp-server-{server}.internal"
    call(
        "PUT",
        f"/v1/routes/{route}/plugin-instances/{STRIP_PLUGIN}",
        {
            "pluginName": STRIP_PLUGIN,
            "enabled": True,
            "rawConfigurations": "reqRules:\n- operate: remove\n  headers:\n  - key: Authorization\n",
        },
    )
    print(f"✓ {server}：认证之后去掉网关凭据")


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


def mcp_server(key: str, *, gateway_only: bool) -> None:
    raw = (HERE / "mcp-serpapi.yaml").read_text(encoding="utf-8").replace("__SERPAPI_KEY__", key)
    auth = gateway_auth() if gateway_only else {"enable": False}
    call(
        "PUT",
        "/v1/mcpServer",
        {
            "name": "serpapi",
            "description": "SerpApi Google 搜索（REST → MCP）",
            "type": "OPEN_API",
            "services": [{"name": "serpapi.dns", "port": 443, "weight": 100}],
            "rawConfigurations": raw,
            "consumerAuthInfo": auth,
        },
    )
    who = "只认 MCP 网关" if gateway_only else "⚠ 未开认证（谁都能直连）"
    print(f"✓ MCP Server serpapi → /mcp-servers/serpapi（{who}）")


def main() -> None:
    init_and_login()
    if len(sys.argv) == 3 and sys.argv[1] == "--strip-credential":
        strip_credential(sys.argv[2])
        return
    enable_mcp()
    credential = os.environ.get("MCP_GATEWAY_CREDENTIAL") or None
    if credential:
        gateway_consumer(credential, os.environ.get("MCP_GATEWAY_CREDENTIAL_PREVIOUS") or None)
    else:
        print("· 未设置 MCP_GATEWAY_CREDENTIAL：MCP 服务不开认证（MCP 网关不会接受它）")
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        print("· 未设置 SERPAPI_KEY，跳过 serpapi（全局配置已完成）")
        return
    service_source()
    mcp_server(key, gateway_only=credential is not None)


if __name__ == "__main__":
    main()
