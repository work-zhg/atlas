"""MCP 网关这一个服务：控制面 + 数据面，共用网关自己的库。

★ 网关凭据只在这里和 Higress 里：不下发给 Atlas，也不进 Pod。
"""

from __future__ import annotations

from gateway_control import GatewayControl, ServicePermissions
from gateway_proxy import GatewayProxy
from gateway_store import GatewayStore
from starlette.applications import Starlette


class Gateway:
    def __init__(self, *, higress_url: str, uc_url: str, credential: str) -> None:
        self.store = GatewayStore()
        self.control = GatewayControl(
            self.store, ServicePermissions(uc_url), higress_url=higress_url, credential=credential
        )
        self.proxy = GatewayProxy(self.control)

    def app(self) -> Starlette:
        return Starlette(routes=[*self.control.internal_routes(), self.proxy.route()])
