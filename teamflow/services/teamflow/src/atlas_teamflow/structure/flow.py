"""流程结构：模板角色表 + 步骤 + 节点。依赖不存，由步骤推出来。

结构的 JSON（模板草稿、模板版本、市场版本里存的都是它）：

    {
      "roles": {"pm": "产品", "dev": "开发"},            # 模板角色：键 → 名字（模板自己的概念）
      "flow": [                                          # 步骤：一个节点，或一个并行组
        {"type": "node", "id": "n1"},
        {"type": "parallel", "tracks": [{"nodes": ["n2"]}, {"nodes": ["n3", "n4"]}]}
      ],
      "nodes": {
        "n1": {"name": "需求澄清", "instructions": "…", "exec_role": "pm",
               "admit": null,                            # 准入：没有上游的节点不配
               "exit": {"roles": ["pm"], "rule": "any"}} # 准出：any 一人同意即过，all 全部同意
      }
    }

★ 节点名在流程里唯一，也是流程空间里放它产出的目录名，所以不能带 / 、不能以 . 开头。
★ 并行组至少两条轨道，每条至少一个节点，不嵌套；并行组的每条轨道接上一步，下一步等所有轨道。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

__all__ = [
    "Issue",
    "ancestors",
    "deps",
    "descendants",
    "downstreams",
    "exec_roles",
    "new_structure",
    "node_ids",
    "normalize",
    "review_roles",
    "roots",
    "validate",
]

Issue = dict[str, Any]
RULES = ("any", "all")
NAME_MAX = 32


def new_structure() -> dict[str, Any]:
    """空白模板：一个空白节点，没有角色。"""
    return {
        "roles": {},
        "flow": [{"type": "node", "id": "n1"}],
        "nodes": {
            "n1": {
                "name": "节点 1",
                "instructions": "",
                "exec_role": "",
                "admit": None,
                "exit": {"roles": [], "rule": "any"},
            }
        },
    }


def _flow_ids(flow: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for step in flow:
        if step.get("type") == "node":
            out.append(step["id"])
        elif step.get("type") == "parallel":
            for track in step.get("tracks") or []:
                out.extend(track.get("nodes") or [])
    return out


def node_ids(structure: dict[str, Any]) -> list[str]:
    """按步骤顺序的全部节点 id。"""
    return _flow_ids(structure.get("flow") or [])


def _review(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    roles = [r for r in raw.get("roles") or [] if isinstance(r, str) and r]
    return {"roles": list(dict.fromkeys(roles)), "rule": raw.get("rule", "any")}


def normalize(structure: dict[str, Any]) -> dict[str, Any]:
    """规范化：删空轨道；只剩一条轨道的并行组拆成顺序步骤；删没出现在步骤里的节点配置。

    结构本身不合法（步骤类型不认识、节点 id 不是字符串）的直接丢掉，留给 validate 报
    「至少一个节点」。
    """
    flow: list[dict[str, Any]] = []
    for step in structure.get("flow") or []:
        if not isinstance(step, dict):
            continue
        if step.get("type") == "node" and isinstance(step.get("id"), str):
            flow.append({"type": "node", "id": step["id"]})
        elif step.get("type") == "parallel":
            tracks = [
                {"nodes": [n for n in (t.get("nodes") or []) if isinstance(n, str)]}
                for t in step.get("tracks") or []
                if isinstance(t, dict)
            ]
            tracks = [t for t in tracks if t["nodes"]]
            if len(tracks) == 1:
                flow.extend({"type": "node", "id": nid} for nid in tracks[0]["nodes"])
            elif len(tracks) >= 2:
                flow.append({"type": "parallel", "tracks": tracks})
    ids = set(_flow_ids(flow))
    nodes: dict[str, Any] = {}
    for nid, raw in (structure.get("nodes") or {}).items():
        if nid not in ids or not isinstance(raw, dict):
            continue
        nodes[nid] = {
            "name": str(raw.get("name") or "").strip(),
            "instructions": str(raw.get("instructions") or ""),
            "exec_role": str(raw.get("exec_role") or ""),
            "admit": _review(raw.get("admit")),
            "exit": _review(raw.get("exit")) or {"roles": [], "rule": "any"},
        }
    roles = {
        str(k): str(v).strip()
        for k, v in (structure.get("roles") or {}).items()
        if isinstance(k, str) and k
    }
    return {"roles": roles, "flow": flow, "nodes": nodes}


def _exits(step: dict[str, Any]) -> list[str]:
    if step["type"] == "node":
        return [step["id"]]
    return [t["nodes"][-1] for t in step["tracks"] if t["nodes"]]


def deps(structure: dict[str, Any]) -> dict[str, list[str]]:
    """节点 → 上游节点。单节点、轨道的第一个节点依赖上一步的全部出口；轨道里后面的节点
    依赖前一个。"""
    out: dict[str, list[str]] = {}
    prev: list[str] = []
    for step in structure.get("flow") or []:
        if step["type"] == "node":
            out[step["id"]] = list(prev)
        else:
            for track in step["tracks"]:
                last = list(prev)
                for nid in track["nodes"]:
                    out[nid] = last
                    last = [nid]
        prev = _exits(step)
    return out


def roots(structure: dict[str, Any]) -> list[str]:
    """没有上游的节点：流程开始时由执行人点「开始」的那些（第一步是并行组时不止一个）。"""
    return [nid for nid, ups in deps(structure).items() if not ups]


def downstreams(structure: dict[str, Any], nid: str) -> list[str]:
    """直接下游。"""
    return [d for d, ups in deps(structure).items() if nid in ups]


def ancestors(structure: dict[str, Any], nid: str) -> set[str]:
    d = deps(structure)
    out: set[str] = set()
    stack = list(d.get(nid, []))
    while stack:
        u = stack.pop()
        if u not in out:
            out.add(u)
            stack.extend(d.get(u, []))
    return out


def descendants(structure: dict[str, Any], nid: str) -> set[str]:
    down: dict[str, list[str]] = defaultdict(list)
    for d, ups in deps(structure).items():
        for u in ups:
            down[u].append(d)
    out: set[str] = set()
    stack = list(down.get(nid, []))
    while stack:
        d = stack.pop()
        if d not in out:
            out.add(d)
            stack.extend(down.get(d, []))
    return out


def exec_roles(structure: dict[str, Any]) -> list[str]:
    """用到的执行角色（按出现顺序去重）。执行角色要有成员和数字员工。"""
    out: list[str] = []
    for nid in node_ids(structure):
        r = ((structure.get("nodes") or {}).get(nid) or {}).get("exec_role") or ""
        if r and r not in out:
            out.append(r)
    return out


def review_roles(structure: dict[str, Any]) -> list[str]:
    """用到的评审角色（按出现顺序去重）。评审角色只要成员。"""
    out: list[str] = []
    for nid in node_ids(structure):
        node = (structure.get("nodes") or {}).get(nid) or {}
        for key in ("admit", "exit"):
            for r in (node.get(key) or {}).get("roles") or []:
                if r not in out:
                    out.append(r)
    return out


def _bad_dir_name(name: str) -> bool:
    return "/" in name or "\\" in name or name.startswith(".") or name in ("..", ".")


def validate(structure: dict[str, Any]) -> tuple[list[Issue], list[Issue]]:
    """发布校验 → (错误, 警告)。错误挡发布；警告只提示。"""
    errors: list[Issue] = []
    warnings: list[Issue] = []

    def err(msg: str, node: str | None = None, field: str | None = None) -> None:
        errors.append({"level": "error", "message": msg, "node": node, "field": field})

    def warn(msg: str, node: str | None = None) -> None:
        warnings.append({"level": "warn", "message": msg, "node": node, "field": None})

    flow = structure.get("flow") or []
    nodes = structure.get("nodes") or {}
    roles = structure.get("roles") or {}

    names_seen: dict[str, str] = {}
    for key, name in roles.items():
        if not name:
            err(f"角色「{key}」没有名字", field="roles")
        elif len(name) > NAME_MAX:
            err(f"角色名「{name}」超过 {NAME_MAX} 个字", field="roles")
        elif name in names_seen:
            err(f"角色名「{name}」重复", field="roles")
        else:
            names_seen[name] = key

    ids = _flow_ids(flow)
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
    if errors:
        return errors, warnings

    upstream = deps(structure)
    node_names: dict[str, str] = {}
    for nid in ids:
        node = nodes.get(nid)
        if node is None:
            err("节点缺少配置", nid)
            continue
        name = (node.get("name") or "").strip()
        label = name or nid
        if not name:
            err("请填写节点名称", nid, "name")
        elif len(name) > NAME_MAX:
            err(f"节点名称最多 {NAME_MAX} 个字", nid, "name")
        elif _bad_dir_name(name):
            err(f"节点名称「{name}」不能带 / 或以 . 开头（它也是流程空间里的目录名）", nid, "name")
        elif name in node_names:
            err(f"节点名称「{name}」重复（它也是流程空间里的目录名）", nid, "name")
        else:
            node_names[name] = nid
        if not (node.get("instructions") or "").strip():
            err(f"「{label}」没有写工作说明", nid, "instructions")
        role = node.get("exec_role") or ""
        if not role:
            err(f"「{label}」没有执行角色", nid, "exec_role")
        elif role not in roles:
            err(f"「{label}」的执行角色不在角色表里", nid, "exec_role")
        exit_r = node.get("exit") or {}
        if not exit_r.get("roles") or exit_r.get("rule") not in RULES:
            err(f"「{label}」的准出评审要配角色和通过规则", nid, "exit")
        else:
            missing = [r for r in exit_r["roles"] if r not in roles]
            if missing:
                err(f"「{label}」的准出评审角色不在角色表里", nid, "exit")
            if exit_r["rule"] == "all" and len(exit_r["roles"]) > 1:
                warn(f"「{label}」的准出要求几个角色的人全部同意", nid)
        admit = node.get("admit")
        if admit is not None:
            if not upstream.get(nid):
                err(f"「{label}」没有上游，不能配准入评审", nid, "admit")
            elif not admit.get("roles") or admit.get("rule") not in RULES:
                err(f"「{label}」的准入评审要配角色和通过规则（或者不配准入）", nid, "admit")
            elif any(r not in roles for r in admit["roles"]):
                err(f"「{label}」的准入评审角色不在角色表里", nid, "admit")
        elif upstream.get(nid):
            warn(f"「{label}」没有配准入评审：上游准出后直接开工", nid)
    return errors, warnings
