"""原生智能体的 Shell 沙箱：命令执行领域的 ShellSandbox（改造计划 B6）。

见 c4-model/agent/sandbox/code-sandbox-exec.html。整套沙箱组件（基座 → 通用供给 → 原生沙箱 →
命令执行）对着假 OpenSandbox（tests/opensandbox_fake.py），
命令真跑在 bash 里；登记在测试库，账本和活动在测试库的 Redis。

被测的是协议与语义：后台提交、重放不重跑、断线用同一个游标接上、沙箱重启 / 替换、init 前断线、
卡死（只信沙箱口径的 CPU）、取消、截断、重建提示与错误语义。真集群上的行为由部署后冒烟覆盖。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from atlas_agent.config import get_settings
from atlas_agent.domain.spec import SandboxSpec
from atlas_agent.ids import uuid7
from atlas_agent.redisx import make_redis
from atlas_agent.sandbox.exec import ExecutionLedger, PollingPolicy, ShellSandbox, StallDetector
from atlas_agent.sandbox.native import WorkspaceRef
from atlas_engine.contracts import SandboxUnavailable

from tests.opensandbox_fake import FakeOpenSandbox
from tests.sandbox_env import component, new_workspace, workspace_dir


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[FakeOpenSandbox]:
    f = FakeOpenSandbox(tmp_path)
    yield f
    f.close()


@pytest.fixture
def ws() -> WorkspaceRef:
    return new_workspace()


@pytest.fixture
async def redis(clean_db: None) -> AsyncIterator[Any]:
    client = make_redis(get_settings())
    yield client
    await client.aclose()


def _sandbox(
    fake: FakeOpenSandbox,
    redis: Any,
    ws: WorkspaceRef,
    *,
    settings: dict[str, Any] | None = None,
    **kw: Any,
) -> ShellSandbox:
    """run_id 随工作区唯一：执行账本在共用的测试 Redis 里，用例之间不能共用 {run}:{tool_call}。"""
    c = component(fake, redis, **(settings or {}))
    kw.setdefault("polling", PollingPolicy(0.02, 0.05))
    kw.setdefault("spec", SandboxSpec())
    return ShellSandbox(
        native=c.native,
        exec=c.exec,
        ledger=c.ledger,
        ws=ws,
        run_id=f"run-{ws.root_thread_uuid}",
        **kw,
    )


async def _until(cond: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < deadline, "等待超时"
        await asyncio.sleep(0.02)


# ──────────────────────────────────────────────── 正常路径


async def test_first_execute_creates_the_sandbox_and_later_ones_reuse_it(
    fake: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    sbx = _sandbox(fake, redis, ws)
    first = await sbx.aexecute("echo hi > a.txt; echo hi")
    second = await sbx.aexecute("cat a.txt; exit 3")
    assert (first.output, first.exit_code) == ("hi\n", 0)
    assert (second.output, second.exit_code) == ("hi\n", 3)
    assert len(fake.created) == 1
    assert (workspace_dir(fake, ws) / "a.txt").read_text() == "hi\n"  # cwd 是 /workspace


async def test_create_request_matches_the_design(
    fake: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    """不过期、标签只写人和 sandbox_uuid、令牌经 env 下发、工作区与技能区按 subPath 挂同一个 PVC、
    带出站策略（strict 档：默认放行，集群网段由 egress 镜像的 deny.always 拒绝）。"""
    await _sandbox(fake, redis, ws).aexecute("true")
    (sb,) = fake.created
    spec = sb.spec
    assert spec["timeout"] is None
    assert spec["image"] == {"uri": get_settings().sandbox_default_image}
    assert set(spec["metadata"]) == {"atlas.managed", "atlas.user", "atlas.sandbox"}
    assert spec["metadata"]["atlas.user"] == str(ws.user_uuid)
    assert spec["networkPolicy"] == {"defaultAction": "allow"}
    assert spec["env"]["HOME"] == "/home/agent"
    mounts = {v["mountPath"]: v for v in spec["volumes"]}
    user, root = ws.user_uuid, ws.root_thread_uuid
    assert mounts["/workspace"]["subPath"] == f"{user}/{root}/workspace"
    assert mounts["/workspace"]["readOnly"] is False
    assert mounts["/skills"]["subPath"] == f"{user}/{root}/skills"
    assert mounts["/skills"]["readOnly"] is True
    assert {v["pvc"]["createIfNotExists"] for v in spec["volumes"]} == {False}
    assert len(spec["env"]["EXECD_ACCESS_TOKEN"]) >= 24


async def test_activity_sees_the_running_command_and_drops_it_after(
    fake: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    sbx = _sandbox(fake, redis, ws)
    c = component(fake, redis)
    task = asyncio.create_task(sbx.aexecute("sleep 0.5"))
    await _until(lambda: fake.created and fake.created[0].commands)
    ref = await c.native.current(ws)
    assert ref is not None
    assert (await c.exec.activity(ref.sandbox_uuid)).busy()  # 跑着的时候闲置回收看得见它
    await task
    assert not (await c.exec.activity(ref.sandbox_uuid)).busy()


async def test_output_keeps_head_and_tail(
    fake: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    r = await _sandbox(fake, redis, ws, output_limit=40).aexecute("seq 1 5000")
    assert r.truncated is True
    assert r.output.startswith("1\n2\n3\n")
    assert r.output.endswith("4999\n5000\n")
    assert "省略中间" in r.output


# ──────────────────────────────────────────────── 重放不重跑


async def test_replayed_tool_call_is_not_run_again(
    fake: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    """跑完了、checkpoint 却没落下 → 同一个 tool_call 再来：从 execd 重读原结果，不重跑。"""
    sbx = _sandbox(fake, redis, ws)
    cmd = "echo x >> marker; echo done; exit 4"
    first = await sbx.aexecute(cmd, exec_id="call-1")
    again = await sbx.aexecute(cmd, exec_id="call-1")
    assert (again.output, again.exit_code) == (first.output, first.exit_code) == ("done\n", 4)
    marker = workspace_dir(fake, ws) / "marker"
    assert marker.read_text() == "x\n"
    other = await sbx.aexecute(cmd, exec_id="call-2")  # 另一个调用照常执行
    assert other.exit_code == 4
    assert marker.read_text() == "x\nx\n"


async def test_replay_while_running_attaches_to_the_same_command(
    fake: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    """另一个实例（新的 ShellSandbox、同一个 Redis）重放同一个调用：经执行账本接上，不重跑。"""
    sbx = _sandbox(fake, redis, ws)
    cmd = "echo x >> marker; sleep 0.4; echo done"
    marker = workspace_dir(fake, ws) / "marker"
    first = asyncio.create_task(sbx.aexecute(cmd, exec_id="call-1"))
    await _until(marker.exists)
    second = await _sandbox(fake, redis, ws).aexecute(cmd, exec_id="call-1")
    assert second.output == "done\n"
    assert (await first).output == "done\n"
    assert marker.read_text() == "x\n"


# ──────────────────────────────────────────────── 断线与沙箱变化


async def test_reconnect_continues_from_the_same_cursor(
    fake: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    """execd 一时连不上：按同一个 sandbox_uuid 重新解析，同一个命令 id、同一个游标接着读。"""
    sbx = _sandbox(fake, redis, ws)
    task = asyncio.create_task(sbx.aexecute("for i in 1 2 3 4 5; do echo line$i; sleep 0.15; done"))
    await _until(lambda: fake.created and fake.created[0].commands)
    await asyncio.sleep(0.25)
    fake.break_execd(1)
    r = await task
    assert r.output == "".join(f"line{i}\n" for i in range(1, 6))
    assert r.exit_code == 0
    assert len(fake.created) == 1


async def test_execd_restart_mid_command_is_reported_not_rerun(
    fake: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    sbx = _sandbox(fake, redis, ws)
    marker = workspace_dir(fake, ws) / "marker"
    task = asyncio.create_task(sbx.aexecute("echo x >> marker; sleep 5"))
    await _until(marker.exists)
    fake.restart_execd(fake.created[0].id)
    with pytest.raises(SandboxUnavailable, match="沙箱重启"):
        await task
    assert marker.read_text() == "x\n"


async def test_replaced_sandbox_fails_the_command_and_is_announced_next_time(
    fake: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    sbx = _sandbox(fake, redis, ws)
    await sbx.aexecute("true")
    task = asyncio.create_task(sbx.aexecute("sleep 5"))
    await _until(lambda: len(fake.created[0].commands) == 2)
    fake.vanish(fake.created[0].id)  # Pod 被驱逐
    with pytest.raises(SandboxUnavailable, match="沙箱重启"):
        await task
    r = await sbx.aexecute("echo ok")  # 下一次执行：供给确认旧的不能用了，退役、建新的
    assert len(fake.created) == 2
    assert r.output.startswith("[沙箱环境已重建")
    assert r.output.endswith("ok\n")
    assert (await sbx.aexecute("echo ok")).output == "ok\n"  # 只提示一次


async def test_disconnect_before_init_reports_unknown_result(
    fake: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    sbx = _sandbox(fake, redis, ws)
    await sbx.aexecute("true")
    fake.drop_init = True
    with pytest.raises(SandboxUnavailable, match="结果未知"):
        await sbx.aexecute("echo x >> marker", exec_id="c")
    marker = workspace_dir(fake, ws) / "marker"
    await _until(marker.exists)
    assert marker.read_text() == "x\n"  # 命令确实跑了 —— 所以不能重跑


async def test_sandbox_start_failure_becomes_tool_error(
    fake: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    fake.create_state = "Failed"
    with pytest.raises(SandboxUnavailable, match="沙箱启动失败"):
        await _sandbox(fake, redis, ws).aexecute("true")
    assert fake.deleted == [fake.created[0].id]  # 起不来的当场删掉


async def test_an_image_outside_the_allowlist_is_a_tool_error(
    fake: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    sbx = _sandbox(fake, redis, ws, spec=SandboxSpec(image="evil/miner:latest"))
    with pytest.raises(SandboxUnavailable, match="不在允许范围内"):
        await sbx.aexecute("true")
    assert fake.created == []


# ──────────────────────────────────────────────── 卡死与取消

_PER_SANDBOX = {"sandbox_usage_scope": "sandbox"}


async def test_quiet_and_idle_command_is_judged_stalled(
    fake: FakeOpenSandbox, redis: Any, ws: WorkspaceRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(StallDetector, "SAMPLE_S", 0.1)
    fake.cpu_pct = 0.5
    sbx = _sandbox(fake, redis, ws, settings=_PER_SANDBOX, idle_s=0.2)
    r = await sbx.aexecute("echo start; sleep 30")
    assert r.output.startswith("start\n")
    assert "判定为卡死" in r.output
    assert r.exit_code != 0


async def test_quiet_but_busy_command_is_not_killed(
    fake: FakeOpenSandbox, redis: Any, ws: WorkspaceRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(StallDetector, "SAMPLE_S", 0.1)
    fake.cpu_pct = 90.0
    sbx = _sandbox(fake, redis, ws, settings=_PER_SANDBOX, idle_s=0.1)
    r = await sbx.aexecute("sleep 0.6; echo done")
    assert (r.output, r.exit_code) == ("done\n", 0)


@pytest.mark.parametrize(
    ("cpu", "scope"),
    [(None, "sandbox"), (0.5, "node")],
    ids=["unknown-cpu", "node-scope-cpu"],
)
async def test_unreliable_cpu_never_kills(
    fake: FakeOpenSandbox,
    redis: Any,
    ws: WorkspaceRef,
    monkeypatch: pytest.MonkeyPatch,
    cpu: float | None,
    scope: str,
) -> None:
    """取不到 CPU，或 CPU 是整个节点的口径（OpenSandbox 1.1.1）：卡死判定不下结论，宁可不杀。"""
    monkeypatch.setattr(StallDetector, "SAMPLE_S", 0.1)
    fake.cpu_pct = cpu
    sbx = _sandbox(fake, redis, ws, settings={"sandbox_usage_scope": scope}, idle_s=0.1)
    r = await sbx.aexecute("sleep 0.6; echo done")
    assert (r.output, r.exit_code) == ("done\n", 0)


async def test_cancel_interrupts_the_command(
    fake: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    sbx = _sandbox(fake, redis, ws)
    task = asyncio.create_task(sbx.aexecute("sleep 30", exec_id="c"))
    await _until(lambda: fake.created and fake.created[0].commands)
    (cmd,) = fake.created[0].commands.values()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _until(lambda: cmd.proc.poll() is not None)
    # ★ 账本里还记着它：重放同一个调用（比如接管的实例）读到的是中断时的输出，不再跑一遍
    again = await sbx.aexecute("sleep 30", exec_id="c")
    assert again.exit_code != 0 and len(fake.created[0].commands) == 1


async def test_cancel_during_submit_still_interrupts(
    fake: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    """取消落在「execd 已经开跑、init 还没回来」之间：拿到 id 后照样中断（不然没人管它）。"""
    fake.submit_delay_s = 0.3
    sbx = _sandbox(fake, redis, ws)
    await sbx.aexecute("true")  # 先把沙箱建好：取消要落在提交上，而不是 ensure 上
    task = asyncio.create_task(sbx.aexecute("sleep 30", exec_id="c"))
    await _until(lambda: len(fake.created[0].commands) == 2)
    cmd = list(fake.created[0].commands.values())[-1]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _until(lambda: cmd.proc.poll() is not None)
    # 提交途中取消：账本里没有它（拿到 id 时调用已经取消），重放就是新的执行
    assert await ExecutionLedger(redis).get(f"run-{ws.root_thread_uuid}", "c") is None


# ──────────────────────────────────────────────── 装配


async def test_assembly_refuses_bash_when_sandbox_disabled() -> None:
    from atlas_agent.errors import CapabilityUnavailable
    from atlas_agent.executor.tool_surface import SandboxNotEnabled

    assert issubclass(SandboxNotEnabled, CapabilityUnavailable)


def test_execute_tool_gets_runtime_injected() -> None:
    """execute 的 runtime 参数必须被 LangChain 识别为注入参数。

    sandbox 中间件曾带着 `from __future__ import annotations`：注解变成字符串，
    ToolRuntime 识别不到，工具一被调用就报 missing positional argument。
    """
    from atlas_engine.kernel.middleware.sandbox import SandboxMiddleware

    box = ShellSandbox(
        native=None,  # type: ignore[arg-type]
        exec=None,  # type: ignore[arg-type]
        ledger=None,  # type: ignore[arg-type]
        ws=WorkspaceRef(uuid7(), uuid7()),
        run_id="r",
        spec=SandboxSpec(),
    )
    tool = SandboxMiddleware(sandbox=box).tools[0]
    assert "runtime" in tool._injected_args_keys
    assert set(tool.tool_call_schema.model_json_schema()["properties"]) == {"command", "timeout"}
