"""会话 Pod 对真 OpenSandbox 的实测（doc/sandbox-opensandbox-design.html §20.1 的 P0-3、P0-6）。

用 Atlas 的沙箱组件（会话 Pod → 通用供给 → 基座）在集群内跑（Pod IP 只在集群网络里可达），真 CLI
镜像；由 opensandbox_probe.sh cli 起一个一次性 Pod 执行。
★ 沙箱先登记再建：要连 DATABASE_URL 指向的库（迁移到最新，模板种子在里面）。

覆盖：建 Pod（带凭据档案 → Credential Vault）并等 bridge 就绪、复用、
bridge 握手（服务令牌来自 entrypoint 写的文件）与错误令牌被拒；
adapter 进程拿到的环境（模型 key、CA、git 配置在，两个令牌都不在）；
用 adapter 的环境 git ls-remote 私有仓库（PROBE_GIT_REPO，空则跳过）；
Pod 没了重建、/state 保留；release。
"""

import asyncio
import os
import time
from types import SimpleNamespace
from uuid import UUID

import httpx
from atlas_agent.cli.client import HostClient
from atlas_agent.config import get_settings
from atlas_agent.credentials import CredentialGrants, CredentialProfileStore, CredentialSubject
from atlas_agent.ids import uuid7
from atlas_agent.redisx import make_redis
from atlas_agent.sandbox.base.execution.values import CommandSpec
from atlas_agent.sandbox.component import SandboxComponent
from atlas_agent.sandbox.provisioning.model import BizRef, BizType
from atlas_engine.contracts.spec import CliSpec

REPO = os.environ.get("PROBE_GIT_REPO", "")
RESULTS = []
#: adapter 进程的 pid（CLI 镜像里没有 ps / pgrep）
ADAPTER_PID = (
    "pid=$(for d in /proc/[0-9]*; do grep -q claude-acp $d/cmdline 2>/dev/null"
    " && echo ${d#/proc/}; done | head -1); "
)


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


async def run(c: SandboxComponent, sid: UUID, cmd: str, timeout: float = 90):
    cid = await c.exec.start(sid, CommandSpec(cmd, cwd="/workspace"))
    out, cur, t0 = b"", 0, time.monotonic()
    while time.monotonic() - t0 < timeout:
        chunk = await c.exec.read(sid, cid, cur)
        out, cur = out + chunk.data, chunk.cursor
        st = await c.exec.status(sid, cid)
        if not st.running:
            chunk = await c.exec.read(sid, cid, cur)
            return (out + chunk.data).decode(errors="replace").strip(), st.exit_code
        await asyncio.sleep(0.3)
    return out.decode(errors="replace"), None


async def noop(*_a, **_k):
    return None


async def main():
    settings = get_settings()
    redis = make_redis(settings)
    c = SandboxComponent.build(settings, redis)
    tid, user = uuid7(), uuid7()
    thread = SimpleNamespace(
        thread_uuid=tid,
        created_by_uuid=user,
        workspace_thread_id=tid,
        credential_profiles=["teamflow-gitee"],
        source_app="teamflow",
    )
    biz = BizRef(BizType.CLI_AGENT, tid, user)
    cli = CliSpec(
        cli_type="claude-code", image=os.environ["CLI_IMAGE"], adapter=os.environ["ADAPTER"]
    )
    # CLI 运行时每轮做的事：凭据组件按这一轮重新授权，交给会话 Pod
    grants = CredentialGrants(CredentialProfileStore.from_settings(settings))
    credentials = grants.resolve(CredentialSubject.of(thread))

    t0 = time.monotonic()
    ep = await c.pods.ensure(thread, cli, credentials=credentials)
    check(
        "ensure（建 Pod + 写 vault + 等 bridge 就绪）",
        ep.url.startswith("ws://"),
        f"{time.monotonic() - t0:.1f}s {ep.url}",
    )
    t1 = time.monotonic()
    again = await c.pods.ensure(thread, cli, credentials=credentials)
    check("复用", again == ep, f"{time.monotonic() - t1:.2f}s")

    # 握手：服务令牌 + 会话 id
    async with HostClient(
        ep.url + "/host", token=ep.token, session_id=str(tid), on_notification=noop, on_request=noop
    ) as hc:
        check("bridge 握手（服务令牌来自 entrypoint 写的文件）", not hc.closed)
    try:
        async with HostClient(
            ep.url + "/host",
            token="wrong",
            session_id=str(tid),
            on_notification=noop,
            on_request=noop,
        ) as hc:
            await asyncio.sleep(0.5)
            check("错误令牌被拒", hc.closed, f"close_code={hc.close_code}")
    except Exception as exc:
        check("错误令牌被拒", True, type(exc).__name__)

    ref = await c.provisioning.current(biz)
    assert ref is not None
    sid = ref.sandbox_uuid
    out, _ = await run(
        c,
        sid,
        ADAPTER_PID + "tr '\\0' '\\n' < /proc/$pid/environ | sed 's/=.*//' | sort | tr '\\n' ' '",
    )
    names = set(out.split())
    assert names, "没找到 adapter 进程"
    check("adapter 拿到模型 key", "ANTHROPIC_AUTH_TOKEN" in names, "")
    check(
        "adapter 拿到 CA 与 git 配置",
        {
            "NODE_EXTRA_CA_CERTS",
            "SSL_CERT_FILE",
            "GIT_SSL_CAINFO",
            "GIT_AUTHOR_NAME",
            "GIT_CONFIG_COUNT",
        }
        <= names,
        "",
    )
    check(
        "adapter 看不到 bridge 令牌 / execd 令牌",
        not ({"ATLAS_BRIDGE_TOKEN", "EXECD_ACCESS_TOKEN"} & names),
        "",
    )
    # ★ 只把 adapter 的这几类变量带过去：git 认的就是 CLI 子进程拿到的那一份
    git_cmd = (
        ADAPTER_PID + "env -i PID=$pid REPO=" + REPO + ' bash -c \'while IFS= read -r -d "" kv; do '
        'case "$kv" in PATH=*|HOME=*|GIT_*|SSL_CERT*|CURL_CA*) export "$kv";; esac; '
        "done < /proc/$PID/environ; git ls-remote $REPO 2>&1 | tail -1'"
    )
    if REPO:
        out, _ = await run(c, sid, git_cmd)
        check(
            "用 adapter 的环境 git ls-remote 私有仓库（Credential Vault）",
            "refs/heads" in out,
            out[:120],
        )
    else:
        print("跳过 git ls-remote（没给 PROBE_GIT_REPO）", flush=True)

    # 状态持久：写 /state → 在 OpenSandbox 上直接删掉 Pod → 下一轮重建 → 还在
    await run(c, sid, "echo resume-me > /state/p0-marker")
    instance = await c.runtime.get(sid)
    async with httpx.AsyncClient(timeout=30, trust_env=False) as hc:
        await hc.delete(
            f"{str(settings.opensandbox_cli_url).rstrip('/')}/v1/sandboxes/{instance.external_id}",
            headers={"OPEN-SANDBOX-API-KEY": settings.opensandbox_cli_api_key.get_secret_value()},
        )
    t2 = time.monotonic()
    await c.pods.ensure(thread, cli, credentials=credentials)
    ref2 = await c.provisioning.current(biz)
    check("Pod 没了 → 重建", ref2 is not None and ref2 != ref, f"{time.monotonic() - t2:.1f}s")
    out, _ = await run(c, ref2.sandbox_uuid, "cat /state/p0-marker")
    check("/state 跨 Pod 保留（CLI resume 的前提）", out == "resume-me", out)

    await c.pods.release(tid, user)
    check("release", await c.runtime.get(ref2.sandbox_uuid) is None, "")
    await redis.aclose()
    failed = [n for n, ok in RESULTS if not ok]
    print(
        f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过"
        + (f"；失败：{failed}" if failed else ""),
        flush=True,
    )


asyncio.run(main())
