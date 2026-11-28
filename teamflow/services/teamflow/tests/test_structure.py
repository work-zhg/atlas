"""共享内核：流程结构的依赖推导和发布校验（纯函数）。"""

from __future__ import annotations

import copy

from atlas_teamflow import structure
from kit import structure as sample


def test_deps_roots_and_descendants() -> None:
    s = sample()
    assert structure.deps(s) == {
        "clarify": [],
        "prd": ["clarify"],
        "code": ["prd"],
        "cases": ["prd"],
    }
    assert structure.roots(s) == ["clarify"]
    assert structure.descendants(s, "clarify") == {"prd", "code", "cases"}
    assert structure.ancestors(s, "code") == {"prd", "clarify"}
    assert sorted(structure.downstreams(s, "prd")) == ["cases", "code"]
    assert structure.exec_roles(s) == ["pm", "dev", "qa"]


def test_sample_is_valid_and_roles_can_both_execute_and_review() -> None:
    errors, warnings = structure.validate(sample())
    assert errors == []
    # PRD 没配准入：上游准出后直接开工（只提示）
    assert [w["node"] for w in warnings] == ["prd"]


def test_validation_errors() -> None:
    s = copy.deepcopy(sample())
    s["nodes"]["clarify"]["admit"] = {"roles": ["pm"], "rule": "any"}
    s["nodes"]["prd"]["name"] = "需求澄清"
    s["nodes"]["code"]["exec_role"] = "ghost"
    s["nodes"]["cases"]["instructions"] = ""
    s["nodes"]["cases"]["exit"] = {"roles": [], "rule": "any"}
    errors, _ = structure.validate(s)
    fields = {(e["node"], e["field"]) for e in errors}
    assert ("clarify", "admit") in fields  # 没有上游的节点不能配准入
    assert ("prd", "name") in fields  # 节点名重复（也是目录名）
    assert ("code", "exec_role") in fields  # 角色不在角色表里
    assert ("cases", "instructions") in fields
    assert ("cases", "exit") in fields


def test_node_name_is_a_directory_name() -> None:
    s = copy.deepcopy(sample())
    s["nodes"]["prd"]["name"] = "a/b"
    errors, _ = structure.validate(s)
    assert any(e["node"] == "prd" and e["field"] == "name" for e in errors)


def test_normalize_drops_empty_tracks_and_orphans() -> None:
    raw = {
        "roles": {"pm": "产品"},
        "flow": [
            {"type": "node", "id": "a"},
            {"type": "parallel", "tracks": [{"nodes": ["b"]}, {"nodes": []}]},
        ],
        "nodes": {"a": {"name": "A"}, "b": {"name": "B"}, "zombie": {"name": "Z"}},
    }
    s = structure.normalize(raw)
    assert s["flow"] == [{"type": "node", "id": "a"}, {"type": "node", "id": "b"}]
    assert set(s["nodes"]) == {"a", "b"}
