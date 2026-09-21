"""子会话的 spec 派生与 `task` 的委派形态。

这两件事一起构成了 subagent 重设计在 engine 侧的全部改动面：

  spec_for_subagent   子会话的 run 用哪份配置跑
  subagent_delegate   `task` 是在图内跑子图，还是把活交出去

红了大多意味着「子会话不再走与普通 run 同一条链路」—— 而那条对称性正是
「子会话 = thread」这个选择的全部回报。
"""

from __future__ import annotations

import pytest
from atlas_engine.contracts import InvalidSpec
from atlas_server.domain.spec import (
    AgentSpec,
    CompactionSpec,
    LimitSpec,
    ModelSpec,
    SkillRefSpec,
    SubAgentSpec,
    spec_for_subagent,
)

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
        limits=LimitSpec(max_steps=17, timeout_s=99, max_subagent_depth=2),
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
    assert child.tool_names == ("filesystem",)
    # slug 带上父的：日志与事件里一眼看出这是谁的哪个子智能体
    assert child.slug == "assistant/coder"


def test_delegation_depth_is_capped_structurally() -> None:
    """子智能体不能再委派 —— 两道保险，不靠提示词。

    会话级模型下嵌套委派会让「哪个会话恢复哪个」变成一棵要遍历的树。
    """
    child = spec_for_subagent(_parent(), "coder")
    assert child.subagents == ()
    assert child.limits.max_subagent_depth == 0


def test_child_carries_its_own_skills_not_the_parents() -> None:
    """技能跟着子智能体走。

    ★ 这条红了意味着子会话会把主 agent 的技能拷进自己的 skills 前缀 ——
      「技能完全隔离」（设计 §03）就没了。
    """
    parent = _parent(skills=(SkillRefSpec("dataviz", 2),))
    child = spec_for_subagent(parent, "coder")

    assert child.skills == (SkillRefSpec("dataviz", 2),)
    assert parent.skills == (SkillRefSpec("writing", 3),)


def test_limits_are_inherited_except_the_depth_cap() -> None:
    """步数、超时、审批名单整体继承 —— 委派不是逃避治理的口子。"""
    child = spec_for_subagent(_parent(), "coder")
    assert child.limits.max_steps == 17
    assert child.limits.timeout_s == 99


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


# ──────────────────────────────────────────────── task 的委派形态


class _Gateway:
    """DelegationProtocol 的测试实现 —— 记录调用并按脚本返回。"""

    def __init__(self, fn):
        self._fn = fn

    async def delegate(self, task: str, name: str, *, fresh: bool = False) -> str:
        return await self._fn(task, name, fresh)


def _task_tool(agent) -> object:
    for tool in agent.nodes["tools"].bound._tools_by_name.values():  # type: ignore[attr-defined]
        if tool.name == "task":
            return tool
    raise AssertionError("图里没有 task 工具")


@pytest.mark.anyio
async def test_task_calls_the_delegate_instead_of_running_a_subgraph() -> None:
    """注入 delegate 后 `task` 只转发三个标量，不跑任何子图。

    ★ 这条是整个改造的接缝。红了说明 kernel 又开始在主图的协程里跑子智能体 ——
      于是子会话、子 run、独立事件流、独立审批全部失效。
    """
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from tests.graphs import build_graph

    seen: list[tuple[str, str, bool]] = []

    async def delegate(description: str, name: str, fresh: bool) -> str:
        seen.append((description, name, fresh))
        return "子智能体的结论"

    agent = build_graph(
        _parent(),
        GenericFakeChatModel(messages=iter([])),
        subagents=_Gateway(delegate),
    )
    tool = _task_tool(agent)

    result = await tool.ainvoke(
        {"description": "修那三个用例", "subagent_type": "coder", "fresh": False}
    )

    assert seen == [("修那三个用例", "coder", False)]
    # 返回的是纯文本，不是 Command —— 子会话的状态归子会话，
    # 主线程只看见结论（设计 §02）。
    assert result == "子智能体的结论"


@pytest.mark.anyio
async def test_fresh_reaches_the_delegate() -> None:
    """`fresh=True` 是模型可用的出口（设计 §04），不能在工具层被吃掉。"""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from tests.graphs import build_graph

    seen: list[bool] = []

    async def delegate(description: str, name: str, fresh: bool) -> str:
        seen.append(fresh)
        return "ok"

    agent = build_graph(
        _parent(), GenericFakeChatModel(messages=iter([])), subagents=_Gateway(delegate)
    )
    await _task_tool(agent).ainvoke(
        {"description": "重来", "subagent_type": "coder", "fresh": True}
    )
    assert seen == [True]


@pytest.mark.anyio
async def test_delegation_failure_becomes_tool_text_not_a_run_failure() -> None:
    """委派失败回成工具错误文本，不冒泡。

    撞并发上限、同轮重名都是**可恢复的局部问题**：主 agent 能改派、
    合并任务或自己动手。让它冒泡等于把一次局部问题升级成全局故障。
    """
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from tests.graphs import build_graph

    async def delegate(description: str, name: str, fresh: bool) -> str:
        msg = "coder 已在本轮中被委派"
        raise RuntimeError(msg)

    agent = build_graph(
        _parent(), GenericFakeChatModel(messages=iter([])), subagents=_Gateway(delegate)
    )
    result = await _task_tool(agent).ainvoke({"description": "x", "subagent_type": "coder"})

    assert isinstance(result, str)
    assert "coder 已在本轮中被委派" in result


def test_delegating_mode_does_not_add_a_general_purpose_subagent() -> None:
    """委派模式下不补 general-purpose。

    它在 server 的 AgentSpec 里没有对应的 SubAgentSpec，委派过去会在解析
    子会话配置时失败 —— 模型看得见一个注定报错的子智能体，每次尝试烧一轮 token。
    """
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from tests.graphs import build_graph

    async def delegate(description: str, name: str, fresh: bool) -> str:
        return ""

    agent = build_graph(
        _parent(), GenericFakeChatModel(messages=iter([])), subagents=_Gateway(delegate)
    )
    assert "general-purpose" not in _task_tool(agent).description


def test_task_description_tells_the_model_subagents_remember() -> None:
    """工具描述必须说清「子智能体记得上次」，否则模型每次重抄背景。

    上游那份描述写着 "Each invocation is stateless" —— 会话级模型下这句
    恰好说反了。图内形态删除后它也一并消失，现在只有这一份描述。
    """
    from atlas_engine.kernel.middleware.subagents import SESSION_TASK_TOOL_DESCRIPTION

    assert "remembers your previous delegations" in SESSION_TASK_TOOL_DESCRIPTION
    # 并行的语义也要写明，否则模型会去并发委派同一个子智能体
    assert "different" in SESSION_TASK_TOOL_DESCRIPTION
    # 反向钉住：图内那句「无状态」不许再回来
    assert "stateless" not in SESSION_TASK_TOOL_DESCRIPTION
