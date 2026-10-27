"""业务系统（mock）：天气查询、工单系统两个 REST 接口。链路的最后一跳。

★ 只认后端凭据（X-Api-Key）：后端凭据只在 Higress 里，所以带着它来的请求一定经过了
  Higress，而 Higress 只认 MCP 网关 —— 请求头里的 X-Auth-Uid 是 MCP 网关写入的，可以信。
  这是设计稿 5.7 的前提：后端自己要只接受带后端凭据的请求，否则 Pod 能绕过网关直接调它。
★ 工单按 X-Auth-Uid（权限主体）记提交人；X-Uid（使用者）只记下来作审计。
"""

from __future__ import annotations

from itertools import count

from hops import hop
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

WEATHER = {"北京": "晴，18~26℃", "上海": "小雨，20~24℃", "杭州": "多云，19~27℃"}


def app(keys: dict[str, str]) -> Starlette:
    """keys：{"weather": 天气接口的后端凭据, "ticket": 工单接口的后端凭据}。"""
    ticket_no = count(1)

    def _denied(request: Request, system: str) -> JSONResponse | None:
        if request.headers.get("x-api-key") == keys[system]:
            return None
        hop(f"业务系统 {system}", "拒绝：没有后端凭据")
        return JSONResponse({"error": "invalid api key"}, status_code=401)

    async def weather(request: Request) -> JSONResponse:
        if denied := _denied(request, "weather"):
            return denied
        city = (await request.json())["city"]
        text = WEATHER.get(city, "没有这个城市的数据")
        hop("业务系统 weather", f"POST /weather city={city} → {text}（后端凭据 ✓）")
        return JSONResponse({"text": text})

    async def tickets(request: Request) -> JSONResponse:
        if denied := _denied(request, "ticket"):
            return denied
        title = (await request.json())["title"]
        creator = request.headers.get("x-auth-uid")
        user = request.headers.get("x-uid")
        no = f"T-{next(ticket_no):03d}"
        hop(
            "业务系统 ticket",
            f"POST /tickets → {no}（后端凭据 ✓；提交人 = X-Auth-Uid {creator}，"
            f"使用者 X-Uid {user} 仅审计）",
        )
        return JSONResponse({"id": no, "text": f"已建工单 {no}「{title}」，提交人 {creator}"})

    return Starlette(
        routes=[
            Route("/weather", weather, methods=["POST"]),
            Route("/tickets", tickets, methods=["POST"]),
        ]
    )
