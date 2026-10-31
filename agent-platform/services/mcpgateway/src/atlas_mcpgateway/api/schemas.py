"""管理接口与内部接口的请求 / 响应。

★ 内部接口（/internal/*）是给 Atlas 的契约：Atlas 侧有一份对应的解析（providers/mcp/gateway.py），
  改字段要两边一起改。
★ 上游凭据不回显：只给类型、不保密的字段和「••••末 4 位」。
★ Key 原文：用户 Key 只在申请、重置时返回一次（KeySecretOut）；智能体 Key 只有内部接口返回。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from ..definitions import SERVICE_NAME_PATTERN

Kind = Literal["mcp", "rest", "grpc"]
Visibility = Literal["public", "private"]
Effect = Literal["read", "write", "destructive"]

# ───────────────────────────── 服务


class CredentialIn(BaseModel):
    """上游凭据。secret 留空 = 沿用原来的（类型没变时）。"""

    type: str = "none"
    key: str | None = None
    where: str | None = None
    user: str | None = None
    token_url: str | None = None
    scope: str | None = None
    cert: str | None = None
    secret: str | None = None


class DiscoverIn(BaseModel):
    kind: Kind
    connection: dict[str, Any]
    credential: CredentialIn = CredentialIn()
    #: gRPC 上传的描述文件：base64 编码的 FileDescriptorSet
    descriptor: str | None = None


class ServiceIn(DiscoverIn):
    name: str = Field(pattern=SERVICE_NAME_PATTERN)
    display_name: str = Field(default="", max_length=100)
    description: str = ""
    visibility: Visibility = "private"
    pass_identity: bool = True
    #: REST：操作（「GET /path」）；gRPC：方法（「包.服务/方法」）。MCP 代理不用挑
    selected: list[str] = []


class ServicePatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=100)
    description: str | None = None
    visibility: Visibility | None = None


class ConnectionIn(BaseModel):
    connection: dict[str, Any]
    pass_identity: bool = True


class ImportIn(BaseModel):
    selected: list[str] = Field(min_length=1)
    descriptor: str | None = None


class RediscoverIn(BaseModel):
    descriptor: str | None = None


class CredentialOut(BaseModel):
    type: str
    key: str | None = None
    where: str | None = None
    user: str | None = None
    token_url: str | None = None
    scope: str | None = None
    cert: str | None = None
    #: 「••••末 4 位」；没有为 None
    hint: str | None = None


class MyAccessOut(BaseModel):
    """当前用户在这个服务上：是不是所有者；他的用户 Key 能用到什么。"""

    owner: bool = False
    #: public = 公开服务；all = 服务级；tools = 工具级；none = 没有授权；no_key = 还没有用户 Key
    scope: str = "none"
    tools: list[str] = []


class ServiceOut(BaseModel):
    name: str
    display_name: str
    description: str
    kind: str
    visibility: str
    enabled: bool
    connection: dict[str, Any]
    credential: CredentialOut
    pass_identity: bool
    created_by: str
    created_at: datetime
    updated_at: datetime
    fetched_at: datetime | None
    fetch_error: str | None
    changed_at: datetime | None
    tool_count: int
    active_tool_count: int
    my: MyAccessOut = MyAccessOut()


class ToolOut(BaseModel):
    name: str
    #: 模型看到的名字（服务名__工具名）；不合规时为空
    model_name: str | None
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] | None
    effect: str
    #: REST / gRPC 的映射；MCP 代理为 None
    mapping: dict[str, Any] | None
    #: active | disabled
    status: str
    issues: list[str]
    digest: str
    changed_at: datetime | None
    previous: dict[str, Any] | None
    #: 能被调用：没停用、没删除、定义合规
    callable: bool


class ServiceDetailOut(ServiceOut):
    tools: list[ToolOut] = []
    removed_tools: list[str] = []
    #: 当前用户能不能改服务定义 / 能不能授权
    can_manage: bool = False
    can_assign: bool = False


class DiscoveredToolOut(BaseModel):
    """发现结果的一项（登记前、重新导入时给管理员挑）。"""

    key: str
    name: str
    description: str
    effect: str
    input_schema: dict[str, Any]
    #: REST：「GET /path」；gRPC：「包.服务/方法」；MCP：工具名
    label: str
    #: gRPC：unary | server | client | bidi
    stream: str | None = None
    unsupported: str | None = None
    issues: list[str] = []
    #: 重新导入时：服务里已经有同名的工具
    exists: bool = False


class DiscoveredOut(BaseModel):
    kind: str
    tools: list[DiscoveredToolOut]
    #: gRPC：描述里的服务
    services: list[str] = []


class ToolIn(BaseModel):
    name: str
    description: str
    effect: Effect = "read"
    mapping: dict[str, Any]


class ToolPatch(BaseModel):
    description: str | None = None
    effect: Effect | None = None
    #: 只有 REST 能改映射
    mapping: dict[str, Any] | None = None


class TryIn(BaseModel):
    key_id: UUID
    arguments: dict[str, Any] = {}


class TryOut(BaseModel):
    """试调：result 和 error 二选一。"""

    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    trace_id: str | None = None


class TestOut(BaseModel):
    ok: bool
    detail: str


# ───────────────────────────── 授权


class KeyRefOut(BaseModel):
    id: UUID
    key_type: str
    name: str
    status: str
    subject_uid: UUID
    client_code: str | None = None


class KeyGrantOut(BaseModel):
    key: KeyRefOut
    #: all = 全部工具（含以后新增的）；tools = 只有 tools 里这几个
    scope: str
    tools: list[str] = []


class OwnerOut(BaseModel):
    user_id: str
    name: str


class ServerGrantsOut(BaseModel):
    owners: list[OwnerOut]
    keys: list[KeyGrantOut]


class GrantIn(BaseModel):
    #: all = 服务级；tools = 工具级；none = 收回
    scope: Literal["all", "tools", "none"]
    tools: list[str] = []


class OwnerIn(BaseModel):
    user_id: UUID


class ServiceGrantIn(GrantIn):
    service: str


class KeyGrantsIn(BaseModel):
    """一次给一把 Key 配多个服务。只改列出来的服务；自己不是所有者的服务不能列。"""

    grants: list[ServiceGrantIn]


class ServiceScopeOut(BaseModel):
    service: str
    display_name: str
    scope: str
    tools: list[str] = []


# ───────────────────────────── API Key


class KeyOut(BaseModel):
    id: UUID
    key_type: str
    #: 用户 Key 的持有人
    user_uid: UUID | None
    client_type: str | None
    client_code: str | None
    name: str
    #: 用户中心里的主体：授权授给它，网关判权时作 X-Auth-Uid
    subject_uid: UUID
    #: 掩码。★ 不返回原文
    masked: str
    status: str
    created_by: str
    created_at: datetime
    rotated_at: datetime
    #: 这把 Key 在私有服务上的权限（列表页显示；取不到时为空）
    grants: list[ServiceScopeOut] = []


class KeySecretOut(KeyOut):
    #: 原文：只在申请、重置用户 Key 时返回这一次
    secret: str


class ClientKeyIn(BaseModel):
    client_type: str = Field(min_length=1, max_length=64)
    client_code: str = Field(min_length=1, max_length=128)
    name: str = Field(default="", max_length=200)
    #: 用户中心主体的负责人（人）。缺省 = 当前操作人
    owner_uid: UUID | None = None


class ClientTypeOut(BaseModel):
    code: str
    apps: list[str]


# ───────────────────────────── 内部接口（给 Atlas）


class MetadataToolOut(BaseModel):
    name: str
    model_name: str | None
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] | None
    read_only: bool
    effect: str
    digest: str
    #: 不能用的原因（含「已停用」）。非空 = Atlas 里不可选、不可调
    issues: list[str]
    #: ok | invalid（沿用 Atlas 的复核状态取值：没有复核了，只剩能用 / 不能用）
    status: str
    changed_at: datetime | None
    previous: dict[str, Any] | None


class MetadataServiceOut(BaseModel):
    name: str
    display_name: str
    description: str
    kind: str
    visibility: str
    enabled: bool
    #: ★ 没有复核了，恒为 False（Atlas 据此不再按复核结论判断）
    review_required: bool = False
    #: ★ 超时属于流量治理（通用组件，单独设计），这里恒为 None
    call_timeout_s: float | None = None
    fetched_at: datetime | None
    fetch_error: str | None
    changed_at: datetime | None
    content_hash: str
    tools: list[MetadataToolOut]
    removed_tools: list[str] = []


class MetadataOut(BaseModel):
    services: list[MetadataServiceOut]


class InternalClientKeyIn(BaseModel):
    client_type: str = Field(min_length=1, max_length=64)
    client_code: str = Field(min_length=1, max_length=128)
    name: str = Field(default="", max_length=200)
    #: 用户中心主体的负责人（人，Atlas 给智能体的负责人）。创建时必填
    owner_uid: UUID | None = None


class UsableServiceOut(BaseModel):
    name: str
    #: 这把 Key 能用的工具（已去掉停用、不合规的）
    tools: list[str]


class InternalClientKeyOut(BaseModel):
    client_type: str
    client_code: str
    name: str
    #: active | blocked
    status: str
    #: 原文；停用时为 None
    key: str | None = None
    key_uid: UUID
    #: 这把 Key 此刻能访问的服务（公开 + 授给它的）和其中能用的工具（可以为空：都停用了）
    servers: list[UsableServiceOut] = []
