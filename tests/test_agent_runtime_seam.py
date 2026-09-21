"""AgentRuntime 接缝（acp 详设 §02）。

这个接缝是 acp 接入的全部支点：**acp 的 run 也是普通 run**，分岔只在
「一轮怎么跑」这一段。红了意味着分岔点漏到了执行器的前后段里 ——
那正是「子会话零改动、web 一行不改」这两条承诺的前提。

测的是**结构性质**，不是执行细节：run_turn 的形状、执行器只问一次
「用哪个 runtime」、以及 runtime 不越界做落库/发布/锁。
"""

from __future__ import annotations

import inspect

from atlas_server.executor import inprocess
from atlas_server.executor.runtime import AgentRuntime, NativeRuntime, select_runtime


def test_native_runtime_satisfies_the_protocol_structurally() -> None:
    """满足协议只需要一个 run_turn，不需要继承。

    acp 的 AcpRuntime 将来同样只是「有这个方法」—— 两个实现之间没有
    共同基类可以被顺手塞进公共逻辑，那正是我们要的。
    """
    assert isinstance(NativeRuntime(assembly=None), AgentRuntime)  # type: ignore[arg-type]


def test_run_turn_is_an_async_generator_not_a_coroutine() -> None:
    """一轮必须**流式**产出事件，不能攒完再返回。

    攒完再返回的话，SSE 要等整轮结束才有第一帧 —— 用户盯着一个不动的
    界面，而模型其实已经在说话了。
    """
    assert inspect.isasyncgenfunction(NativeRuntime.run_turn)


def test_run_turn_takes_per_run_plumbing_as_parameters() -> None:
    """redis / relay 是**每 run 新建**的，必须走参数而不是构造器。

    后台任务不能用请求级连接（创建它的那个 HTTP 请求早已结束）。把它们
    绑在 runtime 实例上，进程级复用的 runtime 就会握着一条早已关闭的连接。
    """
    params = inspect.signature(NativeRuntime.run_turn).parameters
    assert set(params) == {"self", "prepared", "run_id", "redis", "relay"}
    for name in ("run_id", "redis", "relay"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name


def test_executor_asks_which_runtime_exactly_once() -> None:
    """执行器只问一次「用哪个 runtime」—— 步骤 4 加 acp 分支时不用再动它。

    散在多处 if 的话，加一个 agent 类型就要在执行器里找齐所有分支点，
    而漏掉一处的表现是「某条路径下 acp 走了 native 的装配」。
    """
    src = inspect.getsource(inprocess.InProcessExecutor._execute)
    assert src.count("select_runtime(") == 1


def test_runtime_does_not_persist_publish_or_unlock() -> None:
    """runtime 只管跑一轮，不碰落库 / 发布 / 会话锁。

    那三件事在执行器的前后段、两类 run 共用 —— 一旦 runtime 也做，
    acp 接进来就会出现「两套落库路径」，而它们必然漂移。
    """
    src = inspect.getsource(NativeRuntime)
    for forbidden in ("relay.publish", "_persist", "release_thread_lock", "archive_events"):
        assert forbidden not in src, f"NativeRuntime 越界做了 {forbidden}"


def _prepared(kind: str):
    from types import SimpleNamespace

    return SimpleNamespace(spec=SimpleNamespace(kind=kind, slug="a"))


def test_select_runtime_dispatches_on_kind() -> None:
    native = NativeRuntime(assembly=None)  # type: ignore[arg-type]
    acp = object()
    assert select_runtime(_prepared("native"), native=native, acp=acp) is native  # type: ignore[arg-type]
    assert select_runtime(_prepared("acp"), native=native, acp=acp) is acp  # type: ignore[arg-type]


def test_acp_without_a_runtime_is_a_loud_error() -> None:
    """★ 不静默回落 native。

    回落的后果是「配了 CLI 助理，实际跑的是进程内 LangGraph」—— 两者的
    工具面与上下文语义完全不同，而事件流上看不出区别（§13.2）。
    """
    import pytest
    from atlas_engine.contracts import InvalidSpec

    native = NativeRuntime(assembly=None)  # type: ignore[arg-type]
    with pytest.raises(InvalidSpec):
        select_runtime(_prepared("acp"), native=native)
