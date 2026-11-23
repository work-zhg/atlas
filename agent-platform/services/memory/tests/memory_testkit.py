"""记忆服务测试的构造工具（不叫 helpers：避免与别处同名模块撞车）。"""

from __future__ import annotations

from atlas_memory.db.session import get_sessionmaker

_RANK = {"NONE": 0, "READ": 1, "WRITE": 2, "OWNER": 3}

#: 没指定租户的请求都在这个租户里（conftest 的客户端默认带它）
TEST_TENANT = "00000000-0000-0000-0000-00000000000a"
#: 用户 → 所属租户：登记在这里的人不在测试租户里（用户中心替身在别的租户里找不到他）
USERS_ELSEWHERE: dict[str, str] = {}


class FakePermissions:
    """用户中心数据权限的内存替身：(数据编码, 数据标识) → {用户: 级别}。"""

    def __init__(self) -> None:
        self.grants: dict[tuple[str, str], dict[str, str]] = {}
        self.type_levels: dict[tuple[str, str], str] = {}
        self.cleaned: list[tuple[str, str]] = []
        self.down = False

    def _check(self) -> None:
        if self.down:
            from atlas_memory.errors import Unavailable

            raise Unavailable("用户中心暂不可用")

    async def level(self, user_id: str, data_code: str, data_id: str) -> str:
        self._check()
        own = self.grants.get((data_code, data_id), {}).get(user_id, "NONE")
        typ = self.type_levels.get((data_code, user_id), "NONE")
        return own if _RANK[own] >= _RANK[typ] else typ

    async def accessible(
        self, user_id: str, data_code: str, min_level: str = "READ"
    ) -> tuple[set[str], str]:
        self._check()
        ids = {
            did
            for (code, did), users in self.grants.items()
            if code == data_code and _RANK[users.get(user_id, "NONE")] >= _RANK[min_level]
        }
        return ids, self.type_levels.get((data_code, user_id), "NONE")

    async def register(self, data_code: str, data_id: str, data_name: str, owner: str) -> bool:
        self._check()
        if self.grants.get((data_code, data_id)):
            return False
        self.grants[(data_code, data_id)] = {owner: "OWNER"}
        return True

    async def cleanup(self, data_code: str, data_id: str) -> None:
        self._check()
        self.grants.pop((data_code, data_id), None)
        self.cleaned.append((data_code, data_id))


async def add_scope(
    code: str,
    key_fields: list[str],
    *,
    check_permission: bool = False,
    description: str = "",
) -> None:
    from atlas_memory.scopes import ScopeService

    async with get_sessionmaker()() as session:
        await ScopeService(session).create(
            code=code,
            description=description,
            key_fields=key_fields,
            check_permission=check_permission,
            data_code=None,
            actor="test",
        )
        await session.commit()


def ops_request(method: str, path: str, body: object = None) -> dict[str, object]:
    """运营中心的签名请求：→ httpx 的 request 参数（content、headers）。"""
    import json
    from uuid import uuid4

    from atlas_opsapi import OperatorStamp, sign

    raw = b"" if body is None else json.dumps(body).encode()
    stamp = OperatorStamp(operator_name="值班", idempotency_key=uuid4())
    headers = sign(b"test-ops-key", method, path, raw, stamp=stamp)
    if body is not None:
        headers["Content-Type"] = "application/json"
    return {"content": raw, "headers": headers}
