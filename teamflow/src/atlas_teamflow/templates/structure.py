"""流程模板的结构模型（流程模板设计 §5、§6、§9）—— 纯函数，不碰数据库。

流程 = 步骤序列；步骤 = 单个节点 或 并行组；并行组 = ≥ 2 条轨道，
每条轨道 = ≥ 1 个节点的序列；不支持嵌套。
依赖由结构推导，不存储。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "Issue",
    "derive_deps",
    "derive_roles",
    "flow_node_ids",
    "new_definition",
    "normalize",
    "validate",
]

Issue = dict[str, Any]


def new_definition() -> dict[str, Any]:
    """新模板的初始定义：一个空白节点。"""
    return {
        "flow": [{"type": "node", "id": "n1"}],
        "nodes": {
            "n1": {
                "name": "节点 1",
                "exec_role": "",
                "output_name": "",
                "artifact_file_template_id": None,
                "exit_review": {"role": "", "rule": "any"},
                "admit_review": None,
            }
        },
    }


def flow_node_ids(flow: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for step in flow:
        if step.get("type") == "node":
            out.append(step["id"])
        elif step.get("type") == "parallel":
            for track in step.get("tracks") or []:
                out.extend(track.get("nodes") or [])
    return out


def normalize(definition: dict[str, Any]) -> dict[str, Any]:
    """结构规范化（§5.3）：删空轨道；单轨道并行组退化为顺序步骤；删无轨道的并行组；删掉未出现在结构里的节点配置。"""
    flow: list[dict[str, Any]] = []
    for step in definition.get("flow") or []:
        if step.get("type") == "node" and isinstance(step.get("id"), str):
            flow.append({"type": "node", "id": step["id"]})
        elif step.get("type") == "parallel":
            tracks = [
                {"nodes": [n for n in t.get("nodes") or [] if isinstance(n, str)]}
                for t in step.get("tracks") or []
            ]
            tracks = [t for t in tracks if t["nodes"]]
            if len(tracks) == 1:
                flow.extend({"type": "node", "id": nid} for nid in tracks[0]["nodes"])
            elif len(tracks) >= 2:
                flow.append({"type": "parallel", "tracks": tracks})
    ids = set(flow_node_ids(flow))
    nodes = {k: v for k, v in (definition.get("nodes") or {}).items() if k in ids}
    return {"flow": flow, "nodes": nodes}


def _exits(step: dict[str, Any]) -> list[str]:
    if step["type"] == "node":
        return [step["id"]]
    return [t["nodes"][-1] for t in step["tracks"] if t["nodes"]]


def derive_deps(flow: list[dict[str, Any]]) -> dict[str, list[str]]:
    """节点 → 上游节点（§5.2）。

    单节点、轨道首节点依赖上一步全部出口；轨道后续节点依赖同轨道前一个。
    """
    deps: dict[str, list[str]] = {}
    prev: list[str] = []
    for step in flow:
        if step["type"] == "node":
            deps[step["id"]] = list(prev)
        else:
            for track in step["tracks"]:
                last = list(prev)
                for nid in track["nodes"]:
                    deps[nid] = last
                    last = [nid]
        prev = _exits(step)
    return deps


def derive_roles(definition: dict[str, Any]) -> dict[str, list[str]]:
    """发布时推导：执行角色与评审角色（去重，按出现顺序）。"""
    exec_roles: list[str] = []
    review_roles: list[str] = []
    for nid in flow_node_ids(definition.get("flow") or []):
        node = (definition.get("nodes") or {}).get(nid) or {}
        r = (node.get("exec_role") or "").strip()
        if r and r not in exec_roles:
            exec_roles.append(r)
        for key in ("exit_review", "admit_review"):
            rv = node.get(key) or {}
            role = (rv.get("role") or "").strip()
            if role and role not in review_roles:
                review_roles.append(role)
    return {"exec": exec_roles, "review": review_roles}


def validate(
    definition: dict[str, Any], file_templates: dict[str, dict[str, Any]]
) -> tuple[list[Issue], list[Issue]]:
    """发布校验（§9）→ (错误, 警告)。

    file_templates: 文件模板 id → {"name", "status", "has_version"}。
    """
    errors: list[Issue] = []
    warnings: list[Issue] = []
    flow = definition.get("flow") or []
    nodes = definition.get("nodes") or {}

    def err(msg: str, node: str | None = None, field: str | None = None) -> None:
        errors.append({"message": msg, "node": node, "field": field})

    def warn(msg: str, node: str | None = None) -> None:
        warnings.append({"message": msg, "node": node})

    ids = flow_node_ids(flow)
    if not ids:
        err("流程至少要有 1 个节点")
    for step in flow:
        if step.get("type") == "parallel":
            tracks = step.get("tracks") or []
            if len(tracks) < 2:
                err("并行组至少要有 2 条轨道")
            if any(not t.get("nodes") for t in tracks):
                err("并行组的每条轨道至少要有 1 个节点")
        elif step.get("type") != "node":
            err("结构不合法：步骤只能是节点或并行组")
    if len(ids) != len(set(ids)):
        err("节点 id 重复")

    names: dict[str, str] = {}
    outputs: dict[str, str] = {}
    deps = derive_deps(flow) if not errors else {}
    has_downstream = {d for ups in deps.values() for d in ups}
    for nid in ids:
        node = nodes.get(nid)
        if node is None:
            err("节点缺少配置", nid)
            continue
        label = node.get("name") or nid
        name = (node.get("name") or "").strip()
        if not name:
            err("请填写节点名称", nid, "name")
        elif len(name) > 32:
            err("节点名称最多 32 个字", nid, "name")
        elif name in names:
            err(f"节点名称「{name}」重复", nid, "name")
        else:
            names[name] = nid
        if not (node.get("exec_role") or "").strip():
            err(f"「{label}」缺少执行角色", nid, "exec_role")
        out = (node.get("output_name") or "").strip()
        if not out:
            err(f"「{label}」缺少产物名称", nid, "output_name")
        elif out in outputs:
            err(f"产物名称「{out}」重复", nid, "output_name")
        else:
            outputs[out] = nid
        exit_r = node.get("exit_review") or {}
        if not (exit_r.get("role") or "").strip() or exit_r.get("rule") not in ("any", "all"):
            err(f"「{label}」的准出评审需要角色与规则", nid, "exit_review")
        admit = node.get("admit_review")
        if admit is not None and (
            not (admit.get("role") or "").strip() or admit.get("rule") not in ("any", "all")
        ):
            err(f"「{label}」的准入评审需要角色与规则（或不配置准入）", nid, "admit_review")
        ft = node.get("artifact_file_template_id")
        if ft:
            info = file_templates.get(ft)
            if info is None:
                err(f"「{label}」引用的文件模板不存在", nid, "artifact_file_template_id")
            elif info["status"] != "active":
                err(
                    f"「{label}」引用的文件模板「{info['name']}」已停用",
                    nid,
                    "artifact_file_template_id",
                )
            elif not info["has_version"]:
                warn(
                    f"「{label}」引用的文件模板「{info['name']}」还没有生效版本，将按通用格式产出",
                    nid,
                )
        if nid in has_downstream and admit is None:
            warn(f"「{label}」有下游节点但没有配置准入评审", nid)
    roles = derive_roles(definition)
    for r in sorted(set(roles["exec"]) & set(roles["review"])):
        err(f"角色「{r}」不能同时作为执行角色和评审角色（执行角色需要 Agent，评审角色只能是人）")
    return errors, warnings
