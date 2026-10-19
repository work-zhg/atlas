"""接缝上的双写契约 —— 没有类型系统看守的那几条，用测试钉住。

这里的每一条红了都不是「功能坏了」，而是**两处必须一致的东西开始漂移**。
它们的共同点：漂移的直接后果是静默失效（事件消失、协议对不上），
不是报错 —— 所以只能靠测试，等不到运行时炸。
"""

from __future__ import annotations

from atlas_agent.domain.tool_registry import SUBAGENT_TOOL
from atlas_engine import contracts


def test_task_tool_name_matches_the_event_discriminator() -> None:
    """kernel 造出的 task 工具名必须等于 runner 的事件判别常量。

    runner 靠 `call["name"] == SUBAGENT_TOOL` 区分「派 SubAgent」与「普通工具调用」。
    该常量现已单一来源于 kernel/middleware/subagents.py（tool_registry 只
    re-export）——这条测试从「钉双写」降级为回归钉：谁在工具构造里重新
    写死字符串，这里立刻红。
    """
    from atlas_engine.kernel.middleware.subagents import _build_task_tool

    tool = _build_task_tool([{"name": "coder", "description": "写代码", "system_prompt": "x"}])
    assert tool.name == SUBAGENT_TOOL


def test_spawner_protocol_is_published_from_one_place() -> None:
    """kernel 用的 SubAgentSpawner 必须**就是** contracts 里定义的那一个。

    历史教训：`Delegate` 类型曾在 hooks.py 与 kernel 各写一份，内容相同、
    没有任何东西拴着。现在定义唯一地住在 contracts —— 这条断言防的是
    有人在 kernel 里再"就近"定义一份。
    """
    from atlas_engine.kernel.middleware import subagents

    assert contracts.SubAgentSpawner is subagents.SubAgentSpawner


def test_subagent_service_satisfies_the_protocol() -> None:
    """server 的实现结构性满足协议 —— isinstance 查方法存在，
    真实的参数形状由 test_subagent_sessions.py 的端到端调用钉住。
    """
    from types import SimpleNamespace
    from uuid import uuid4

    from atlas_agent.domain.spec import AgentSpec, ModelSpec
    from atlas_agent.services.subagent import SubAgentService

    thread = SimpleNamespace(id=uuid4(), agent_id=uuid4(), created_by=uuid4())
    service = SubAgentService(
        None,  # type: ignore[arg-type] — isinstance 只查方法，不会真跑
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        parent_run_id=uuid4(),
        parent_thread=thread,  # type: ignore[arg-type]
        parent_spec=AgentSpec(
            slug="a", name="a", system_prompt="", model=ModelSpec(model="claude-sonnet-5")
        ),
        agent_version_id=uuid4(),
    )
    assert isinstance(service, contracts.SubAgentSpawner)


def test_runner_state_keys_exist_in_kernel_schemas() -> None:
    """runner 模式匹配的状态键必须在 kernel 的状态 schema 里真实存在。

    runner.py::_from_updates 用 `payload.get("todos")` 推导 todos.updated 事件 ——
    这是 runner 与上游 langchain 之间一份**没有类型的隐式契约**，改键名的后果
    是事件静默消失。（file.written 不走 state：文件在对象存储，由
    FilesystemMiddleware 经 custom stream 补发，见 test_tool_gating。）
    """
    from langchain.agents.middleware import TodoListMiddleware

    todo_keys = TodoListMiddleware().state_schema.__annotations__
    assert "todos" in todo_keys, "上游的待办状态键变了，runner 的 todos.updated 会静默消失"


def test_contracts_exports_are_importable_and_declared() -> None:
    """contracts 的 __all__ 与实际可导入的名字一致 —— re-export 断了要立刻红。

    kernel 裁剪时删掉一个被 contracts 引用的符号，报错点会在「谁先 import
    contracts」处 —— 这条测试把它固定成套件里最早、指向最明确的那个。
    """
    for name in contracts.__all__:
        assert getattr(contracts, name, None) is not None, f"contracts.{name} 不可用"
