"""工具定义：规范化、机械检查、定义指纹、复核状态（MCP 方案设计 §3.3、§4.2–§4.3、§6.4）。

工具定义（名字、描述、参数说明）会原样进入模型上下文，它本身就是一段第三方文本。

定义指纹覆盖名字、描述、参数定义和只读注解：只读注解决定审批是否直接放行（§6.4），
服务把一个工具改标为只读，必须像改了描述一样触发复核。

★ 指纹的过渡（§12.1）：没标只读的工具，指纹和此前 Atlas 自己算的完全一样
  （sha256([name, description, schema])）；标了只读的才把注解算进去。所以上线后
  只有只读工具的指纹会变一次 —— 它们要重新复核一次，而它们恰好是审批直接放行的那一批；
  其余工具的复核结论、Agent 保存时记下的指纹都不受影响。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "MAX_DESCRIPTION_CHARS",
    "MAX_PARAM_DESCRIPTION_CHARS",
    "SERVICE_NAME_PATTERN",
    "Definition",
    "ToolStatus",
    "content_hash",
    "digest_of",
    "model_name",
    "normalize",
    "tool_status",
]

#: 服务名：小写字母开头、不含下划线（模型看到的名字是「服务名__工具名」，第一个 __ 就是分隔点），
#: 不超过 24 个字符（§3.2）
SERVICE_NAME_PATTERN = r"^[a-z][a-z0-9-]{0,23}$"
#: 各模型厂商工具名规则的共同子集
_MODEL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: 描述超长本身就是可疑信号（§4.2）
MAX_DESCRIPTION_CHARS = 2000
MAX_PARAM_DESCRIPTION_CHARS = 1000
#: 注入特征。★ 与配置服务的技能扫描同一份规则（atlas_config/skill/scanner.py「提示词注入特征」），
#:   各自一份、不抽公共包；另加工具描述特有的「调用本工具前请先……」
_INJECTION = re.compile(
    r"(?i)(ignore\s+(all\s+)?(previous|prior|above)\s+instructions"
    r"|disregard\s+(the\s+)?system\s+prompt|you\s+are\s+now\s+"
    r"|before\s+(calling|using)\s+(this|any)\s+tool"
    r"|忽略(之前|以上|前面|上述)的?(所有)?(指令|指示|规则)|你现在是"
    r"|(调用|使用)(本|这个|此)工具(之)?前[，,\s]*(请|必须|务必)?先)"
)

ToolStatus = Literal["ok", "invalid", "pending_review", "rejected"]


def model_name(service: str, tool: str) -> str | None:
    """模型看到的名字。线上名不合规（字符集 / 超长）时返回 None。"""
    name = f"{service}__{tool}"
    return name if _MODEL_NAME.match(name) else None


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def read_only(annotations: dict[str, Any] | None) -> bool:
    return (annotations or {}).get("readOnlyHint") is True


def digest_of(
    name: str, description: str, schema: dict[str, Any], annotations: dict[str, Any] | None
) -> str:
    parts: list[Any] = [name, description, schema]
    if read_only(annotations):
        parts.append({"readOnlyHint": True})
    return _sha(_canonical(parts))


def content_hash(digests: list[str]) -> str:
    return _sha(_canonical(sorted(digests)))


@dataclass(frozen=True)
class Definition:
    """一个工具的定义，已规范化（MCP 线上格式的注解）。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] | None
    digest: str
    issues: tuple[str, ...]

    def as_previous(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "input_schema": self.input_schema,
            "annotations": self.annotations,
            "digest": self.digest,
        }


def _param_issues(schema: dict[str, Any]) -> list[str]:
    out: list[str] = []
    props = schema.get("properties")
    if not isinstance(props, dict):
        return out
    for key, prop in props.items():
        text = prop.get("description") if isinstance(prop, dict) else None
        if isinstance(text, str):
            if len(text) > MAX_PARAM_DESCRIPTION_CHARS:
                out.append(
                    f"参数 {key} 的描述过长（{len(text)} > {MAX_PARAM_DESCRIPTION_CHARS} 字符）"
                )
            if _INJECTION.search(text):
                out.append(f"参数 {key} 的描述含注入特征")
    return out


def normalize(service: str, raw: dict[str, Any]) -> Definition:
    """MCP 线上格式的工具（tools/list 返回里的一项）→ Definition，顺带做机械检查（§4.2）。"""
    name = str(raw.get("name") or "")
    description = str(raw.get("description") or "")
    schema = raw.get("inputSchema")
    schema = dict(schema) if isinstance(schema, dict) else {}
    annotations = raw.get("annotations")
    annotations = (
        {k: v for k, v in annotations.items() if v is not None}
        if isinstance(annotations, dict)
        else None
    )
    issues: list[str] = []
    if model_name(service, name) is None:
        issues.append(f"工具名 {name!r} 不符合模型侧命名规则（[A-Za-z0-9_-]，总长 ≤64）")
    if len(description) > MAX_DESCRIPTION_CHARS:
        issues.append(f"描述过长（{len(description)} > {MAX_DESCRIPTION_CHARS} 字符）")
    if _INJECTION.search(description):
        issues.append("描述含注入特征")
    if schema.get("type") != "object":
        issues.append("参数定义不是对象（inputSchema.type 不是 object）")
    issues.extend(_param_issues(schema))
    return Definition(
        name=name,
        description=description,
        input_schema=schema,
        annotations=annotations or None,
        digest=digest_of(name, description, schema, annotations),
        issues=tuple(issues),
    )


def tool_status(
    *, issues: list[str] | tuple[str, ...], review_required: bool, decision: str | None
) -> ToolStatus:
    """只有 ok 的工具能被选、能进模型上下文、能经网关调用（§4.3）。

    decision：这一版定义（按指纹）的复核结论，没有为 None。
    """
    if issues:
        return "invalid"
    if decision == "rejected":
        return "rejected"
    if decision == "approved" or not review_required:
        return "ok"
    return "pending_review"
