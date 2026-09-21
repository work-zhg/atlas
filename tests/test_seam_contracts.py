"""接缝上的双写契约 —— 没有类型系统看守的那几条，用测试钉住。

这里的每一条红了都不是「功能坏了」，而是**两处必须一致的东西开始漂移**。
它们的共同点：漂移的直接后果是静默失效（事件消失、协议对不上），
不是报错 —— 所以只能靠测试，等不到运行时炸。
"""

from __future__ import annotations

from atlas_engine import contracts
from atlas_server.domain.tool_registry import SUBAGENT_TOOL


class _NullGateway:
    async def delegate(self, task: str, name: str, *, fresh: bool = False) -> str:
        return ""


def test_task_tool_name_matches_the_event_discriminator() -> None:
    """kernel 造出的委派工具名必须等于 runner 的事件判别常量。

    runner 靠 `call["name"] == SUBAGENT_TOOL` 区分「委派」与「普通工具调用」。
    该常量现已单一来源于 kernel/middleware/subagents.py（tool_registry 只
    re-export）——这条测试从「钉双写」降级为回归钉：谁在工具构造里重新
    写死字符串，这里立刻红。
    """
    from atlas_engine.kernel.middleware.subagents import _build_delegating_task_tool

    tool = _build_delegating_task_tool(
        [{"name": "coder", "description": "写代码", "system_prompt": "x"}],
        _NullGateway(),
        None,
    )
    assert tool.name == SUBAGENT_TOOL


def test_delegation_protocol_is_published_from_one_place() -> None:
    """kernel 用的 DelegationProtocol 必须**就是** contracts 里定义的那一个。

    历史教训：`Delegate` 类型曾在 hooks.py 与 kernel 各写一份，内容相同、
    没有任何东西拴着。现在定义唯一地住在 contracts —— 这条断言防的是
    有人在 kernel 里再"就近"定义一份。
    """
    from atlas_engine.kernel.middleware import subagents

    assert contracts.DelegationProtocol is subagents.DelegationProtocol


def test_subagent_service_satisfies_the_protocol() -> None:
    """server 的实现结构性满足协议 —— isinstance 查方法存在，
    真实的参数形状由 test_subagent_sessions.py 的端到端调用钉住。
    """
    from types import SimpleNamespace
    from uuid import uuid4

    from atlas_server.domain.spec import AgentSpec, ModelSpec
    from atlas_server.services.subagent import SubagentService

    thread = SimpleNamespace(id=uuid4(), agent_id=uuid4(), created_by=uuid4())
    service = SubagentService(
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
    assert isinstance(service, contracts.DelegationProtocol)


def test_runner_state_keys_exist_in_kernel_schemas() -> None:
    """runner 模式匹配的状态键必须在 kernel 的状态 schema 里真实存在。

    runner.py::_from_updates 用 `payload.get("todos")` / `payload.get("files")`
    推导 todos.updated 与 file.written 事件 —— 这是 runner 与 kernel 之间
    一份**没有类型的隐式契约**。kernel（或上游 langchain）改键名的后果
    同样是事件静默消失。
    """
    from langchain.agents.middleware import TodoListMiddleware

    from atlas_engine.kernel.middleware.filesystem import FilesystemMiddleware

    fs_keys = FilesystemMiddleware(backend=None).state_schema.__annotations__
    assert "files" in fs_keys, "kernel 的文件状态键变了，runner 的 file.written 会静默消失"

    todo_keys = TodoListMiddleware().state_schema.__annotations__
    assert "todos" in todo_keys, "上游的待办状态键变了，runner 的 todos.updated 会静默消失"


def test_contracts_exports_are_importable_and_declared() -> None:
    """contracts 的 __all__ 与实际可导入的名字一致 —— re-export 断了要立刻红。

    kernel 裁剪时删掉一个被 contracts 引用的符号，报错点会在「谁先 import
    contracts」处 —— 这条测试把它固定成套件里最早、指向最明确的那个。
    """
    for name in contracts.__all__:
        assert getattr(contracts, name, None) is not None, f"contracts.{name} 不可用"
