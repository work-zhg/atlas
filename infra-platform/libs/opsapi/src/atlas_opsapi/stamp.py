"""OperatorStamp：写进每个运营请求的「谁、为了哪次操作」。

运营中心签名时一起带上（签进签名原文，改不了），业务系统据此写日志，和运营中心的审计对上；
不按幂等键去重：创建、删除本身就能重做。
名字可能是中文，请求头只能放 latin-1，所以按 URL 编码传。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote, unquote
from uuid import UUID

__all__ = ["IDEMPOTENCY", "OPERATION", "OPERATOR", "OPERATOR_NAME", "OperatorStamp"]

OPERATOR = "X-Ops-Operator"
OPERATOR_NAME = "X-Ops-Operator-Name"
OPERATION = "X-Ops-Operation"
IDEMPOTENCY = "Idempotency-Key"


def _uuid(raw: str | None) -> UUID | None:
    if not raw:
        return None
    try:
        return UUID(raw.strip())
    except ValueError:
        return None


@dataclass(frozen=True)
class OperatorStamp:
    #: 运营中心的运营账号；后台任务自动发起时（如到期停用）为空
    operator_uuid: UUID | None = None
    operator_name: str = ""
    #: 运营中心的租户操作；不属于某次租户操作的调用（改参数、看用量）为空
    operation_uuid: UUID | None = None
    #: 步骤 uuid：重做时对方按它去重；只读请求为空
    idempotency_key: UUID | None = None

    @property
    def who(self) -> str:
        """记进修订历史、运营记录里的操作人。"""
        if self.operator_name:
            return f"运营：{self.operator_name}"
        return "运营中心" if self.operator_uuid is None else f"运营：{self.operator_uuid}"

    def to_headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        if self.operator_uuid:
            h[OPERATOR] = str(self.operator_uuid)
        if self.operator_name:
            h[OPERATOR_NAME] = quote(self.operator_name)
        if self.operation_uuid:
            h[OPERATION] = str(self.operation_uuid)
        if self.idempotency_key:
            h[IDEMPOTENCY] = str(self.idempotency_key)
        return h

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> OperatorStamp:
        def get(name: str) -> str | None:
            return headers.get(name) or headers.get(name.lower())

        return cls(
            operator_uuid=_uuid(get(OPERATOR)),
            operator_name=unquote(get(OPERATOR_NAME) or ""),
            operation_uuid=_uuid(get(OPERATION)),
            idempotency_key=_uuid(get(IDEMPOTENCY)),
        )
