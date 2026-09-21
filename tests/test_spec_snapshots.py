"""版本快照的 schema 演进守卫。

agent_version.spec 是**历史数据**：executor 每次执行都会
`AgentSpecIn.model_validate(...)` 反序列化它（inprocess.py）。

给这些模型加一个**必填**字段，所有历史快照当场炸掉 —— 历史 run 打不开，
而 CI 抓不到：测试库里只有新格式。可复现性承诺
（run 绑 agent_version_id，§5.4）在那一刻静默失效。

规则：**新字段必须有默认值**。本文件把它从口头纪律变成机械守卫：
  · test_required_fields_are_pinned 钉住每个模型的必填集合，加必填就红
  · test_v1_snapshot_still_loads 用 0002 迁移种下的真实 v1 格式做回归
"""

from __future__ import annotations

from atlas_server.schemas.agent import (
    AgentSpecIn,
    SkillRefIn,
    CompactionSpecIn,
    LimitSpecIn,
    ModelSpecIn,
    SubAgentSpecIn,
)

# ★ 允许的必填集合。想给某个模型加必填字段时这个测试会拦住你 ——
#   停下来想清楚：历史快照里没有这个字段，反序列化会直接失败。
#   正确做法是给默认值；若语义上真的"必须由用户提供"，
#   在 API 层校验新请求，而不是在存储模型上加必填。
_PINNED_REQUIRED: dict[type, set[str]] = {
    AgentSpecIn: {"model"},
    ModelSpecIn: {"model"},
    LimitSpecIn: set(),
    CompactionSpecIn: set(),
    # subagents 列表整体有默认值，但列表**里面**的对象也是历史数据 ——
    # 老快照里存过的 SubAgentSpecIn 同样不能新增必填
    SubAgentSpecIn: {"name", "model"},
    # 技能引用：version 必须可缺省（"用当前版本"），由服务层在落库前解析
    SkillRefIn: {"slug"},
}


def test_required_fields_are_pinned() -> None:
    for model_cls, pinned in _PINNED_REQUIRED.items():
        actual = {name for name, f in model_cls.model_fields.items() if f.is_required()}
        assert actual == pinned, (
            f"{model_cls.__name__} 的必填字段从 {sorted(pinned)} 变成了 {sorted(actual)}。\n"
            f"历史快照（agent_version.spec）里没有新字段，"
            f"反序列化会失败 —— 新字段请给默认值，见本文件模块注释。"
        )


# 0002 迁移种下的内置智能体 spec，原样照抄 —— 这是库里真实存在的最老格式。
# ★ 不要随 schema 演进更新这份字典：它的意义正是「老格式必须永远能读」。
_V1_BUILTIN_SPEC = {
    "system_prompt": (
        "你是 Atlas 的通用助手。回答简洁、直接，不确定时明确说明。\n"
        "需要多步骤的任务先用 write_todos 拆解再执行。"
    ),
    "model": {
        "model": "claude-sonnet-5",
        "provider": "anthropic",
        "effort": None,
        "thinking": "auto",
        "max_output_tokens": 16384,
        "prompt_cache": True,
        "temperature": None,
    },
    "tool_names": ["write_todos", "filesystem"],
    "subagents": [],
    "limits": {
        "max_steps": 40,
        "timeout_s": 300,
        "max_total_tokens": 500000,
        "max_subagent_depth": 2,
        "tool_concurrency": 4,
        "require_approval_for": [],
    },
    "compaction": {
        "enabled": True,
        "trigger_ratio": 0.75,
        "target_ratio": 0.40,
        "keep_recent_turns": 3,
        "summarizer_model": "claude-haiku-4-5",
    },
}

# P2 时代的最小形态：只有必填项，其余全靠默认值补齐
_V1_MINIMAL_SPEC = {
    "system_prompt": "p",
    "model": {"model": "claude-opus-5"},
}


def test_v1_snapshot_still_loads() -> None:
    """老格式反序列化 + 转成 engine spec + 校验，三步都要通。"""
    for raw in (_V1_BUILTIN_SPEC, _V1_MINIMAL_SPEC):
        spec = AgentSpecIn.model_validate(raw).to_engine(slug="legacy", name="旧快照")
        spec.validate()
        assert spec.model.model.startswith("claude-")
