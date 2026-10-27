"""子会话的 spec 派生与 `task` 的形态。

这两件事一起构成了 SubAgent 重设计在 engine 侧的全部改动面：

  spec_for_subagent   子会话的 run 用哪份配置跑
  SubAgentConfig      `task` 是在图内跑子图，还是让 spawner 起子 run

红了大多意味着「子会话不再走与普通 run 同一条链路」—— 而那条对称性正是
「子会话 = thread」这个选择的全部回报。
"""

from __future__ import annotations

import pytest
from atlas_agent.domain.spec import (
    AgentSpec,
    CompactionSpec,
    LimitSpec,
    ModelSpec,
    SkillRefSpec,
    SubAgentSpec,
    spec_for_subagent,
)
from atlas_engine.contracts import InvalidSpec

MODEL = ModelSpec(model="claude-sonnet-5")
HAIKU = ModelSpec(model="claude-haiku-4-5")


def _parent(**sub_kwargs) -> AgentSpec:
    return AgentSpec(
        slug="assistant",
        name="助理",
        system_prompt="你是主 agent",
        model=MODEL,
        tool_names=("filesystem", "bash"),
        skills=(SkillRefSpec("writing", 3),),
        limits=LimitSpec(max_steps=17, timeout_s=99),
        compaction=CompactionSpec(trigger_ratio=0.8, target_ratio=0.3),
        subagents=(
            SubAgentSpec(
                name="coder",
                description="写代码",
                system_prompt="你是 coder",
                model=HAIKU,
                tool_names=("filesystem",),
                **sub_kwargs,
            ),
        ),
    )


# ──────────────────────────────────────────────── spec 派生


def test_child_spec_is_a_full_agent_spec() -> None:
    """子会话拿到的是一份**完整的** AgentSpec，不是半个配置对象。

    这正是子 run 能复用普通 run 全部链路的前提：执行器不需要为它开
    一条分支，`_prepare` 只是换了一份 spec。
    """
    child = spec_for_subagent(_parent(), "coder")

    assert isinstance(child, AgentSpec)
    assert child.name == "coder"
    assert child.system_prompt == "你是 coder"
    assert child.model == HAIKU
    # 自己的工具 + task（native SubAgent 带着名录，可以继续派 SubAgent）
    assert child.tool_names == ("filesystem", "task")
    # slug 带上父的：日志与事件里一眼看出这是谁的哪个子智能体
    assert child.slug == "assistant/coder"


def test_a_native_subagent_keeps_the_roster() -> None:
    """★ native SubAgent 带着**同一份名录**：它也装 task，可以继续派 SubAgent，层数不设上限。

    名录只有根 agent 版本快照里这一份，孙 run 也从根快照按名字派生 —— 每一层看到同一张表。
    跑飞的递归由整棵树的累计上限兜住（SubAgentService），不靠深度。
    """
    parent = _parent()
    child = spec_for_subagent(parent, "coder")
    assert child.subagents == parent.subagents
    assert spec_for_subagent(child, "coder").subagents == parent.subagents


def test_an_acp_subagent_has_no_roster() -> None:
    """acp SubAgent 不带名录：它的工具面由 CLI 自带，task 装不进它的图（AgentSpec.validate）。"""
    from atlas_agent.domain.spec import CliSpec

    child = spec_for_subagent(_parent(kind="acp", cli=CliSpec(cli_type="claude-code")), "coder")
    assert child.subagents == ()


def test_child_carries_its_own_skills_not_the_parents() -> None:
    """技能跟着子智能体走。

    ★ 这条红了意味着子会话会把主 agent 的技能拷进自己的 skills 前缀 ——
      「技能完全隔离」（设计 §03）就没了。
    """
    parent = _parent(skills=(SkillRefSpec("dataviz", 2),))
    child = spec_for_subagent(parent, "coder")

    assert child.skills == (SkillRefSpec("dataviz", 2),)
    assert parent.skills == (SkillRefSpec("writing", 3),)


def test_limits_are_inherited() -> None:
    """步数、超时、审批名单整体继承 —— 派 SubAgent 不是逃避治理的口子。"""
    parent = _parent()
    child = spec_for_subagent(parent, "coder")
    assert child.limits == parent.limits


def test_compaction_falls_back_to_the_parents() -> None:
    """没给就继承（与 model 的处理方式一致）；给了就用自己的。"""
    inherited = spec_for_subagent(_parent(), "coder")
    assert inherited.compaction.trigger_ratio == 0.8

    own = CompactionSpec(trigger_ratio=0.5, target_ratio=0.2)
    overridden = spec_for_subagent(_parent(compaction=own), "coder")
    assert overridden.compaction is own


def test_unknown_subagent_is_a_loud_error() -> None:
    """名字对不上必须炸，不能回落到主 agent 的配置。

    静默回落的表现是：子会话用主 agent 的提示词和全套工具跑起来了，
    而事件流上看不出任何异常。
    """
    with pytest.raises(InvalidSpec):
        spec_for_subagent(_parent(), "researcher")


def test_session_mode_defaults_to_persistent() -> None:
    """连续性是本次改造的目的，所以它是默认值而不是开关。"""
    assert _parent().subagents[0].session_mode == "persistent"


# ──────────────────────────────────────────────── task 的形态


def _caller(**over) -> object:
    """会派 coder 的父模型：第一轮发 task。"""
    import json

    from langchain_core.messages import AIMessageChunk

    from .fakes import TurnModel, tool_call_chunk

    args = {"description": "修用例", "prompt": "修那三个用例", "subagent_type": "coder", **over}
    return TurnModel(
        scripts=[
            [tool_call_chunk("task", json.dumps(args, ensure_ascii=False), "c1")],
            [AIMessageChunk(content="好了")],
        ]
    )


@pytest.mark.anyio
async def test_task_spawns_a_sub_run_instead_of_running_a_subgraph() -> None:
    """`task` 只把三个标量交给 spawner，不跑任何子图，本段随即挂起。

    ★ 这条是整个改造的接缝。红了说明 kernel 又开始在主图的协程里跑 SubAgent ——
      于是子会话、子 run、独立事件、独立审批全部失效。
    """
    from uuid import uuid4

    from atlas_agent.domain.events import EventType

    from .fakes import Spawner
    from .graphs import run_agent

    spawner = Spawner()
    events = [
        e
        async for e in run_agent(
            _parent(), _caller(), run_id=uuid4(), input_content="修一下", spawner=spawner
        )
    ]
    assert spawner.calls == [("coder", "修那三个用例", False)]
    assert events[-1].type is EventType.RUN_SUSPENDED


@pytest.mark.anyio
async def test_fresh_reaches_the_spawner() -> None:
    """`fresh=True` 是模型可用的出口（设计 §04），不能在工具层被吃掉。"""
    from uuid import uuid4

    from .fakes import Spawner
    from .graphs import run_agent

    spawner = Spawner()
    async for _ in run_agent(
        _parent(), _caller(fresh=True), run_id=uuid4(), input_content="重来", spawner=spawner
    ):
        pass
    assert spawner.calls[0][2] is True


@pytest.mark.anyio
async def test_the_task_tool_body_never_runs() -> None:
    """★ task 的全部行为在 SubAgentMiddleware.awrap_tool_call 里，工具函数只提供 schema。

    它被直接执行就说明中间件没接住 —— 比如有人把 SubAgentMiddleware 挪出了工具调用链。
    """
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from .fakes import Spawner
    from .graphs import build_graph

    agent = build_graph(_parent(), GenericFakeChatModel(messages=iter([])), spawner=Spawner())
    with pytest.raises(RuntimeError, match="SubAgentMiddleware"):
        await _task_tool(agent).ainvoke(
            {"description": "x", "prompt": "x", "subagent_type": "coder"}
        )


def test_task_schema_separates_title_and_brief() -> None:
    """标题（description）与任务书（prompt）是两个必填字段。

    ★ 只有一个 description 时，deepseek-v4-pro 把它当标题填，SubAgent 只收到一句话
      （回放对照见 TaskToolSchema 的说明）。红了说明有人把两个字段又并了回去。
    """
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from .fakes import Spawner
    from .graphs import build_graph

    agent = build_graph(_parent(), GenericFakeChatModel(messages=iter([])), spawner=Spawner())
    schema = _task_tool(agent).tool_call_schema.model_json_schema()
    assert {"description", "prompt", "subagent_type"} <= set(schema["required"])


@pytest.mark.anyio
async def test_brief_falls_back_to_description_without_prompt() -> None:
    """只给了 description 的调用（拆字段之前的形态）：把它当任务书交出，不交空串。"""
    import json
    from uuid import uuid4

    from .fakes import Spawner, TurnModel, tool_call_chunk
    from .graphs import run_agent

    old = json.dumps({"description": "修那三个用例", "subagent_type": "coder"}, ensure_ascii=False)
    spawner = Spawner()
    async for _ in run_agent(
        _parent(),
        TurnModel(scripts=[[tool_call_chunk("task", old, "c1")]]),
        run_id=uuid4(),
        input_content="修一下",
        spawner=spawner,
    ):
        pass
    assert spawner.calls == [("coder", "修那三个用例", False)]


def _task_tool(agent) -> object:
    for tool in agent.nodes["tools"].bound._tools_by_name.values():  # type: ignore[attr-defined]
        if tool.name == "task":
            return tool
    raise AssertionError("图里没有 task 工具")


def test_no_general_purpose_subagent_is_added() -> None:
    """不补 general-purpose。

    它在 server 的 AgentSpec 里没有对应的 SubAgentSpec，派过去会在解析
    子会话配置时失败 —— 模型看得见一个注定报错的 SubAgent，每次尝试烧一轮 token。
    """
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from .fakes import Spawner
    from .graphs import build_graph

    agent = build_graph(_parent(), GenericFakeChatModel(messages=iter([])), spawner=Spawner())
    assert "general-purpose" not in _task_tool(agent).description


def test_task_description_tells_the_model_subagents_remember() -> None:
    """工具描述必须说清「子智能体记得上次」，否则模型每次重抄背景。

    上游那份描述写着 "Each invocation is stateless" —— 会话级模型下这句
    恰好说反了。图内形态删除后它也一并消失，现在只有这一份描述。
    """
    from atlas_engine.kernel.middleware.subagents import SESSION_TASK_TOOL_DESCRIPTION

    assert "remembers your previous delegations" in SESSION_TASK_TOOL_DESCRIPTION
    # 并行的语义按模式写明：持久的同轮串行，一次性的可以并行
    assert "same persistent subagent in one turn are rejected" in SESSION_TASK_TOOL_DESCRIPTION
    assert "same ephemeral subagent several times in one turn" in SESSION_TASK_TOOL_DESCRIPTION
    # 反向钉住：图内那句「无状态」不许再回来
    assert "stateless" not in SESSION_TASK_TOOL_DESCRIPTION


def test_task_description_teaches_how_to_brief() -> None:
    """交代任务的写法：像给新同事交代、写明目的、要多长的回复、不外包理解。"""
    from atlas_engine.kernel.middleware.subagents import SESSION_TASK_TOOL_DESCRIPTION as d

    assert "smart colleague" in d
    assert "purpose" in d
    assert "length of report" in d
    assert "Never delegate understanding" in d


def test_agents_block_marks_session_mode() -> None:
    """每个子智能体标出模式；缺省按 persistent。"""
    from atlas_engine.kernel.middleware.subagents import _agents_block

    block = _agents_block(
        [
            {"name": "coder", "description": "写代码"},
            {"name": "scout", "description": "调研", "session_mode": "ephemeral"},
        ]
    )
    assert "- coder [persistent]: 写代码" in block
    assert "- scout [ephemeral]: 调研" in block
