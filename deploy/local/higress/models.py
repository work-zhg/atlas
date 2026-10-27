"""把 Higress 配置成模型网关的上游（doc/model-design.html §4.1、§9.3）。可重复执行。

    MODEL_GATEWAY_CREDENTIAL=... DEEPSEEK_API_KEY=... python deploy/local/higress/models.py \\
        --provider deepseek --type deepseek --token-env DEEPSEEK_API_KEY \\
        --model deepseek-chat --model deepseek-reasoner

每个供应商一次，做三件事（全走控制台 REST API，同 setup.py）：
  1. 消费者 model-gateway 持有网关凭据（和 MCP 网关的 mcp-gateway 分开，各用各的）
  2. AI 服务提供者：类型、供应商 Key（只在 Higress 里）；--custom-url 给 OpenAI 兼容的自部署服务
  3. AI 路由 models-<供应商>：按请求体里的模型名匹配这些模型，只允许 model-gateway 调用

本机 2.2.4 实测（2026-10-27）：不带网关凭据 401；带网关凭据转发时，Authorization 被换成供应商 Key
（网关凭据不会流到供应商）；向 OpenAI 协议的供应商发 /v1/messages，Higress 自动转成
/v1/chat/completions 再转回 Anthropic 格式，流式也一样，用量在 message_delta 里。

★ 只配同一个模型的上游，不配换模型的回退（§9.4）：Higress 看不到授权，换模型的回退等于绕过授权。
★ 轮换网关凭据不停机：先 MODEL_GATEWAY_CREDENTIAL=新 MODEL_GATEWAY_CREDENTIAL_PREVIOUS=旧 跑一次
  （两把都认），模型网关切到新的之后，去掉 PREVIOUS 再跑一次。
★ 配好之后在模型网关登记（python -m atlas_modelgateway model add … 或 import），上游模型名就是这里的
  --model；登记时的接入检查会确认这条路由不带凭据调不通。

只用标准库：不依赖项目虚拟环境，任何 python3 都能跑。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from setup import call, init_and_login, unwrap_or_none  # noqa: E402

GATEWAY_CONSUMER = "model-gateway"


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


def provider(name: str, kind: str, token: str, custom_url: str | None) -> None:
    raw: dict[str, object] = {}
    if custom_url:
        raw["openaiCustomUrl"] = custom_url
    body = {
        "name": name,
        "type": kind,
        "tokens": [token],
        "protocol": "openai/v1",
        **({"rawConfigs": raw} if raw else {}),
    }
    if unwrap_or_none(call("GET", f"/v1/ai/providers/{name}", missing_ok=True)) is None:
        call("POST", "/v1/ai/providers", body)
    else:
        call("PUT", f"/v1/ai/providers/{name}", body)
    print(f"✓ AI 服务提供者 {name}（{kind}）")


def route(name: str, models: list[str]) -> None:
    route_name = f"models-{name}"
    body = {
        "name": route_name,
        "domains": [],
        "pathPredicate": {"matchType": "PRE", "matchValue": "/", "caseSensitive": False},
        "upstreams": [{"provider": name, "weight": 100, "modelMapping": {}}],
        "modelPredicates": [{"matchType": "EQUAL", "matchValue": m} for m in models],
        # ★ 只认模型网关（§9.3）
        "authConfig": {
            "enabled": True,
            "allowedCredentialTypes": ["key-auth"],
            "allowedConsumers": [GATEWAY_CONSUMER],
        },
    }
    existing = unwrap_or_none(call("GET", f"/v1/ai/routes/{route_name}", missing_ok=True))
    if existing is None:
        call("POST", "/v1/ai/routes", body)
    else:
        call("PUT", f"/v1/ai/routes/{route_name}", {**body, "version": existing.get("version")})  # type: ignore[union-attr]
    print(f"✓ AI 路由 {route_name}：{', '.join(models)}（只认 {GATEWAY_CONSUMER}）")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", required=True, help="供应商名（Higress 上的提供者名）")
    parser.add_argument("--type", required=True, help="Higress 的提供者类型：deepseek / qwen / openai …")
    parser.add_argument("--token-env", required=True, help="供应商 Key 所在的环境变量")
    parser.add_argument("--model", action="append", required=True, help="上游模型名（可重复）")
    parser.add_argument("--custom-url", help="OpenAI 兼容的自部署服务地址（type=openai 时）")
    args = parser.parse_args()

    credential = os.environ.get("MODEL_GATEWAY_CREDENTIAL") or ""
    if not credential:
        sys.exit("要设置 MODEL_GATEWAY_CREDENTIAL：模型路由只认模型网关，没有网关凭据谁都调不了")
    token = os.environ.get(args.token_env) or ""
    if not token:
        sys.exit(f"环境变量 {args.token_env} 为空（供应商 Key）")
    init_and_login()
    gateway_consumer(credential, os.environ.get("MODEL_GATEWAY_CREDENTIAL_PREVIOUS") or None)
    provider(args.provider, args.type, token, args.custom_url)
    route(args.provider, args.model)


if __name__ == "__main__":
    main()
