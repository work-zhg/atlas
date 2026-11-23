"""ClientTypePolicy：客户端类型 → 能建、取、删这一类 Key 的应用（按请求头 X-Caller-App 认）。

现在只有 Agent → atlas（settings.client_types）。管理员可以在页面上为已登记类型的客户端补申请。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence

from ..errors import Forbidden, NotFound

__all__ = ["ClientTypePolicy"]


class ClientTypePolicy:
    def __init__(self, client_types: Mapping[str, Sequence[str]]) -> None:
        self._types = client_types

    def known(self, client_type: str) -> bool:
        return client_type in self._types

    def items(self) -> Iterator[tuple[str, Sequence[str]]]:
        return iter(self._types.items())

    def require(self, client_type: str, app: str) -> None:
        """只有客户端类型的所属应用能调。"""
        apps = self._types.get(client_type)
        if apps is None:
            raise NotFound(f"没有登记的客户端类型：{client_type}", code="CLIENT_TYPE_UNKNOWN")
        if app not in apps:
            raise Forbidden(
                f"应用 {app or '（未知）'} 不能操作客户端类型 {client_type}",
                code="CLIENT_TYPE_FORBIDDEN",
            )
