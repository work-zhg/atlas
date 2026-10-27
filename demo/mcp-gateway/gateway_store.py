"""MCP 网关的库（mock，放内存）：注册表、工具元数据与复核结论、API Key、访问日志。

控制面写，数据面读。Atlas 读不到这里，只能走网关的内部接口（gateway_control.internal_routes）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

#: 描述超长本身就是可疑信号（设计稿 4.2 机械检查）
MAX_DESCRIPTION = 2000


@dataclass
class Service:
    name: str  # 稳定的服务名：工具前缀、用户中心里的数据标识
    title: str
    upstream: str  # 上游：Higress 上的服务名（替换 Higress 的接缝）
    review_required: bool
    enabled: bool = True


@dataclass
class ToolMeta:
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    digest: str
    issues: list[str] = field(default_factory=list)


def digest_of(tool: dict[str, Any]) -> str:
    """定义指纹：名字、描述、参数定义、只读注解（设计稿 4.3）。tool 是 MCP 线上格式的工具定义。"""
    parts = [
        tool["name"],
        tool.get("description") or "",
        tool.get("inputSchema") or {},
        (tool.get("annotations") or {}).get("readOnlyHint") is True,
    ]
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def tool_meta(tool: dict[str, Any]) -> ToolMeta:
    issues = []
    if len(tool.get("description") or "") > MAX_DESCRIPTION:
        issues.append("描述过长")
    if (tool.get("inputSchema") or {}).get("type") != "object":
        issues.append("参数定义不是对象")
    return ToolMeta(
        name=tool["name"],
        description=tool.get("description") or "",
        input_schema=tool.get("inputSchema") or {},
        read_only=(tool.get("annotations") or {}).get("readOnlyHint") is True,
        digest=digest_of(tool),
        issues=issues,
    )


@dataclass
class GatewayStore:
    services: dict[str, Service] = field(default_factory=dict)
    #: 服务 → 工具名 → 元数据（上游拉来的定义，平台级，所有人共用一份）
    tools: dict[str, dict[str, ToolMeta]] = field(default_factory=dict)
    #: (服务, 工具, 指纹) → approved / rejected。批准的是某一版定义，不是工具本身
    reviews: dict[tuple[str, str, str], str] = field(default_factory=dict)
    #: API Key → 主体（人或数字员工）；一个主体最多一把
    keys: dict[str, str] = field(default_factory=dict)
    access_log: list[dict[str, Any]] = field(default_factory=list)

    def status(self, service: str, tool: ToolMeta) -> str:
        """available / pending_review / rejected / unusable。只有 available 能选、能调。"""
        if tool.issues:
            return "unusable"
        if not self.services[service].review_required:
            return "available"
        verdict = self.reviews.get((service, tool.name, tool.digest))
        return {"approved": "available", "rejected": "rejected"}.get(
            verdict or "", "pending_review"
        )

    def allows(self, service: str, name: str, digest: str | None = None) -> bool:
        """数据面执行复核：这个工具能不能进列表、能不能被调。

        digest 给了（tools/list 的返回里这版定义的指纹），还要和库里复核过的那版一致 ——
        上游悄悄改了定义、网关还没刷新时，指纹对不上，一样不放行。
        """
        tool = self.tools.get(service, {}).get(name)
        if tool is None or self.status(service, tool) != "available":
            return False
        return digest is None or digest == tool.digest

    def key_of(self, subject: str) -> str | None:
        return next((k for k, s in self.keys.items() if s == subject), None)
