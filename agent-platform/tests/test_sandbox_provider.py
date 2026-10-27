"""server 侧的 Shell 沙箱：PodSandbox 对着假 OpenSandbox（doc/sandbox-opensandbox-design.html §8）。

假 lifecycle + 假 execd 在进程内（tests/opensandbox_fake.py），命令真跑在 bash 里；
路由表、令牌、锁用测试库的 Redis。

被测的是协议与语义：后台提交、重放不重跑、断线用同一个游标接上、沙箱重启 / 替换、
init 前断线、卡死、取消、截断、重建提示与错误语义。
真集群上的行为由 P0 验证与部署后冒烟覆盖。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from atlas_agent.config import get_settings
from atlas_agent.providers import sandbox as sandbox_mod
from atlas_agent.providers.opensandbox import SandboxProvisioner
from atlas_agent.providers.sandbox import PodSandbox
from atlas_agent.redisx import make_redis
from atlas_engine.contracts import SandboxUnavailable

from tests.opensandbox_fake import FakeOpenSandbox, fake_settings, wipe

USER = "u-1"


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[FakeOpenSandbox]:
    f = FakeOpenSandbox(tmp_path)
    yield f
    f.close()


@pytest.fixture
def ws() -> str:
    return f"ws-{uuid4().hex[:10]}"


@pytest.fixture
async def redis(fake: FakeOpenSandbox, ws: str) -> AsyncIterator[Any]:
    client = make_redis(get_settings())
    yield client
    await wipe(client, ws, fake.prefix)
    await client.aclose()


def _provisioner(fake: FakeOpenSandbox, redis: Any, **settings: Any) -> SandboxProvisioner:
    return SandboxProvisioner(fake_settings(**settings), redis, transport=fake.async_transport)


def _sandbox(fake: FakeOpenSandbox, redis: Any, ws: str, **kw: Any) -> PodSandbox:
    """run_id 随工作区唯一：命令映射是进程级的，用例之间不能共用 {run}:{tool_call}。"""
    kw.setdefault("poll_min_s", 0.02)
    kw.setdefault("poll_max_s", 0.05)
    return PodSandbox(
        provisioner=_provisioner(fake, redis),
        workspace_thread_id=ws,
        user_id=USER,
        run_id=f"run-{ws}",
        transport=fake.async_transport,
        **kw,
    )


async def _until(cond: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < deadline, "等待超时"
        await asyncio.sleep(0.02)


# ──────────────────────────────────────────────── 正常路径


async def test_first_execute_creates_the_sandbox_and_later_ones_reuse_it(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    sbx = _sandbox(fake, redis, ws)
    first = await sbx.aexecute("echo hi > a.txt; echo hi")
    second = await sbx.aexecute("cat a.txt; exit 3")
    assert (first.output, first.exit_code) == ("hi\n", 0)
    assert (second.output, second.exit_code) == ("hi\n", 3)
    assert len(fake.created) == 1
    assert (fake.workspace(ws) / "a.txt").read_text() == "hi\n"  # cwd 是 /workspace


async def test_create_request_matches_the_design(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    """§8.1：不过期、元数据可按会话 / 用户查、令牌经 env 下发、
    工作区与技能区按 subPath 挂同一个 PVC。
    """
    await _sandbox(fake, redis, ws).aexecute("true")
    (sb,) = fake.created
    spec = sb.spec
    assert spec["timeout"] is None
    assert spec["image"] == {"uri": get_settings().sandbox_default_image}
    assert spec["metadata"] == {
        "atlas.managed": "true",
        "atlas.kind": "sbx",
        "atlas.thread": ws,
        "atlas.user": USER,
    }
    mounts = {v["mountPath"]: v for v in spec["volumes"]}
    assert mounts["/workspace"]["subPath"] == f"{USER}/{ws}/workspace"
    assert mounts["/workspace"]["readOnly"] is False
    assert mounts["/skills"]["subPath"] == f"{USER}/{ws}/skills"
    assert mounts["/skills"]["readOnly"] is True
    assert {v["pvc"]["createIfNotExists"] for v in spec["volumes"]} == {False}
    token = spec["env"]["EXECD_ACCESS_TOKEN"]
    assert len(token) >= 24
    assert await redis.get(f"atlas:sbx:token:{sb.id}") == token  # GET 不回 env：只能 Atlas 自己存


async def test_running_set_is_empty_after_the_command(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    sbx = _sandbox(fake, redis, ws)
    task = asyncio.create_task(sbx.aexecute("sleep 0.5"))
    for _ in range(100):  # 跑着的时候闲置回收看得见它
        if await sbx._p.routes.running(ws):
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("运行中的命令没有记进路由表")
    await task
    assert await sbx._p.routes.running(ws) == set()


async def test_output_keeps_head_and_tail(fake: FakeOpenSandbox, redis: Any, ws: str) -> None:
    r = await _sandbox(fake, redis, ws, output_limit=40).aexecute("seq 1 5000")
    assert r.truncated is True
    assert r.output.startswith("1\n2\n3\n")
    assert r.output.endswith("4999\n5000\n")
    assert "省略中间" in r.output


# ──────────────────────────────────────────────── 重放不重跑


async def test_replayed_tool_call_is_not_run_again(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    """跑完了、checkpoint 却没落下 → 同一个 tool_call 再来：从 execd 重读原结果，不重跑。"""
    sbx = _sandbox(fake, redis, ws)
    cmd = "echo x >> marker; echo done; exit 4"
    first = await sbx.aexecute(cmd, exec_id="call-1")
    again = await sbx.aexecute(cmd, exec_id="call-1")
    assert (again.output, again.exit_code) == (first.output, first.exit_code) == ("done\n", 4)
    assert (fake.workspace(ws) / "marker").read_text() == "x\n"
    other = await sbx.aexecute(cmd, exec_id="call-2")  # 另一个调用照常执行
    assert other.exit_code == 4
    assert (fake.workspace(ws) / "marker").read_text() == "x\nx\n"


async def test_replay_while_running_attaches_to_the_same_command(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    sbx = _sandbox(fake, redis, ws)
    cmd = "echo x >> marker; sleep 0.4; echo done"
    first = asyncio.create_task(sbx.aexecute(cmd, exec_id="call-1"))
    await _until(lambda: (fake.workspace(ws) / "marker").exists())
    second = await _sandbox(fake, redis, ws).aexecute(cmd, exec_id="call-1")
    assert second.output == "done\n"
    assert (await first).output == "done\n"
    assert (fake.workspace(ws) / "marker").read_text() == "x\n"


# ──────────────────────────────────────────────── 断线与沙箱变化


async def test_reconnect_continues_from_the_same_cursor(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    """execd 一时连不上：重新 ensure，同一个命令 id、同一个游标接着读 —— 输出不重不漏。"""
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
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    sbx = _sandbox(fake, redis, ws)
    task = asyncio.create_task(sbx.aexecute("echo x >> marker; sleep 5"))
    await _until(lambda: (fake.workspace(ws) / "marker").exists())
    fake.restart_execd(fake.created[0].id)
    with pytest.raises(SandboxUnavailable, match="沙箱重启"):
        await task
    assert (fake.workspace(ws) / "marker").read_text() == "x\n"


async def test_replaced_sandbox_fails_the_command_and_is_announced_next_time(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    sbx = _sandbox(fake, redis, ws)
    await sbx.aexecute("true")
    task = asyncio.create_task(sbx.aexecute("sleep 5"))
    await _until(lambda: len(fake.created[0].commands) == 2)
    fake.vanish(fake.created[0].id)  # Pod 被驱逐：重连时 ensure 建了新沙箱
    with pytest.raises(SandboxUnavailable, match="沙箱重启"):
        await task
    assert len(fake.created) == 2
    r = await sbx.aexecute("echo ok")
    assert r.output.startswith("[沙箱环境已重建")
    assert r.output.endswith("ok\n")
    assert (await sbx.aexecute("echo ok")).output == "ok\n"  # 只提示一次


async def test_disconnect_before_init_reports_unknown_result(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    fake.drop_init = True
    with pytest.raises(SandboxUnavailable, match="结果未知"):
        await _sandbox(fake, redis, ws).aexecute("echo x >> marker", exec_id="c")
    await _until(lambda: (fake.workspace(ws) / "marker").exists())
    assert (fake.workspace(ws) / "marker").read_text() == "x\n"  # 命令确实跑了 —— 所以不能重跑


async def test_sandbox_start_failure_becomes_tool_error(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    fake.create_state = "Failed"
    with pytest.raises(SandboxUnavailable, match="沙箱启动失败"):
        await _sandbox(fake, redis, ws).aexecute("true")
    assert fake.deleted == [fake.created[0].id]  # 起不来的当场删掉


# ──────────────────────────────────────────────── 卡死与取消


async def test_quiet_and_idle_command_is_judged_stalled(
    fake: FakeOpenSandbox, redis: Any, ws: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox_mod, "_CPU_SAMPLE_S", 0.1)
    fake.cpu_pct = 0.5
    r = await _sandbox(fake, redis, ws, idle_s=0.2).aexecute("echo start; sleep 30")
    assert r.output.startswith("start\n")
    assert "判定为卡死" in r.output
    assert r.exit_code != 0


async def test_quiet_but_busy_command_is_not_killed(
    fake: FakeOpenSandbox, redis: Any, ws: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox_mod, "_CPU_SAMPLE_S", 0.1)
    fake.cpu_pct = 90.0
    r = await _sandbox(fake, redis, ws, idle_s=0.1).aexecute("sleep 0.6; echo done")
    assert (r.output, r.exit_code) == ("done\n", 0)


async def test_unknown_cpu_never_kills(
    fake: FakeOpenSandbox, redis: Any, ws: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox_mod, "_CPU_SAMPLE_S", 0.1)
    fake.cpu_pct = None
    r = await _sandbox(fake, redis, ws, idle_s=0.1).aexecute("sleep 0.6; echo done")
    assert (r.output, r.exit_code) == ("done\n", 0)


async def test_cancel_interrupts_the_command(fake: FakeOpenSandbox, redis: Any, ws: str) -> None:
    sbx = _sandbox(fake, redis, ws)
    task = asyncio.create_task(sbx.aexecute("sleep 30", exec_id="c"))
    await _until(lambda: fake.created and fake.created[0].commands)
    (cmd,) = fake.created[0].commands.values()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _until(lambda: cmd.proc.poll() is not None)
    assert f"run-{ws}:c" not in sandbox_mod._EXECS  # 中断过的调用再来一次就是新的执行


async def test_cancel_during_submit_still_interrupts(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    """取消落在「execd 已经开跑、init 还没回来」之间：拿到 id 后照样中断。

    否则命令会在沙箱里一直跑下去，没人管（不设运行上限）。
    """
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
    assert f"run-{ws}:c" not in sandbox_mod._EXECS


# ──────────────────────────────────────────────── 装配


async def test_assembly_refuses_bash_when_sandbox_disabled() -> None:
    from atlas_agent.errors import CapabilityUnavailable
    from atlas_agent.executor.assembly import SandboxNotEnabled

    assert issubclass(SandboxNotEnabled, CapabilityUnavailable)


def test_execute_tool_gets_runtime_injected() -> None:
    """execute 的 runtime 参数必须被 LangChain 识别为注入参数。

    sandbox 中间件曾带着 `from __future__ import annotations`：注解变成字符串，
    ToolRuntime 识别不到，工具一被调用就报 missing positional argument ——
    沙箱接入前它从没真正装配过，评测冒烟才撞出来。
    """
    from atlas_engine.kernel.middleware.sandbox import SandboxMiddleware

    box = PodSandbox(
        provisioner=None,  # type: ignore[arg-type]
        workspace_thread_id="ws",
        user_id=USER,
        run_id="r",
    )
    tool = SandboxMiddleware(sandbox=box).tools[0]
    assert "runtime" in tool._injected_args_keys
    assert set(tool.tool_call_schema.model_json_schema()["properties"]) == {"command", "timeout"}
