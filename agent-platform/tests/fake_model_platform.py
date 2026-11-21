"""模型管理平台的替身（Atlas 只经平台取模型 Key 之后，所有跑模型的测试都接它）。

conftest 的自动夹具把它接到 ModelPlatformClient 上：任何客户端 Key 都是可用的，分配了 MODELS 里的
全部模型。测平台本身（待分配、停用、模型没分配）的用例自己换一个 transport（见
test_model_platform_atlas.py 的 FakePlatform）。
"""

from __future__ import annotations

import json

import httpx

from tests.fakes import JUDGE_MODEL

#: 测试里用到的模型名（智能体、子智能体、判断器）。用了别的名字就加在这里
MODELS = (
    "claude-fable-5",
    "claude-haiku-4-5",
    "claude-opus-5",
    "claude-sonnet-5",
    "deepseek-chat",
    "deepseek-flash",
    "deepseek-v4-pro",
    "m",
    JUDGE_MODEL,
)
#: 替身发的 Key 原文（假模型不看它）
KEY = "sk-test"


class FakeModelPlatform:
    """平台的内部接口（/internal/*）：客户端 Key 一律可用、分配了全部模型。"""

    def __init__(self) -> None:
        self.catalog = [{"name": m, "display_name": m, "provider": "test"} for m in MODELS]

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/models":
            return httpx.Response(200, json={"models": self.catalog})
        if request.method == "GET":
            slot = (request.url.params["client_type"], request.url.params["client_code"])
        else:
            body = json.loads(request.content)
            slot = (body["client_type"], body["client_code"])
        return httpx.Response(
            200,
            json={
                "client_type": slot[0],
                "client_code": slot[1],
                "name": slot[1],
                "status": "active",
                "key": KEY,
                "models": self.catalog,
            },
        )
