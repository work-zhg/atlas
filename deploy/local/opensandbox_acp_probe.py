"""ACP Pod 对真 OpenSandbox 的实测（doc/sandbox-opensandbox-design.html §20.1 的 P0-3、P0-6）。

用 OpenSandboxPods / AcpProvisioner 在集群内跑（Pod IP 只在集群网络里可达），真 CLI 镜像；
由 opensandbox_probe.sh acp 起一个一次性 Pod 执行。

覆盖：建 Pod（带 git 档案 → Credential Vault）并等 bridge 就绪、复用、
bridge 握手（令牌来自 entrypoint 写的文件）与错误令牌被拒；
adapter 进程拿到的环境（模型 key、CA、git 配置在，令牌不在）；
用 adapter 的环境 git ls-remote 私有仓库（PROBE_GIT_REPO，空则跳过）；
Pod 没了重建、/state 保留；release。
"""

import asyncio
import os
import time
import uuid
from types import SimpleNamespace

import httpx
from atlas_agent.config import get_settings
from atlas_agent.domain.spec import CliSpec
from atlas_agent.host.client import HostClient
from atlas_agent.providers.opensandbox import AcpProvisioner, ExecdClient, OpenSandboxPods
from atlas_agent.redisx import make_redis

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


async def run(execd, cmd, timeout=90):
    cid = await execd.start(cmd, cwd="/workspace")
    out, cur, t0 = b"", 0, time.monotonic()
    while time.monotonic() - t0 < timeout:
        c, cur = await execd.logs(cid, cur)
        out += c
        st = await execd.status(cid)
        if not st.get("running"):
            c, cur = await execd.logs(cid, cur)
            return (out + c).decode(errors="replace").strip(), st.get("exit_code")
        await asyncio.sleep(0.3)
    return out.decode(errors="replace"), None


async def noop(*_a, **_k):
    return None


async def main():
    settings = get_settings()
    redis = make_redis(settings)
    pods = OpenSandboxPods(settings, redis)
    prov = AcpProvisioner(settings, redis)
    tid = f"p0acp-{uuid.uuid4().hex[:8]}"
    thread = SimpleNamespace(
        id=tid,
        created_by="p0-user",
        workspace_thread_id=tid,
        git_profile="teamflow-gitee",
        source_app="teamflow",
    )
    cli = CliSpec(
        cli_type="claude-code", image=os.environ["CLI_IMAGE"], adapter=os.environ["ADAPTER"]
    )

    t0 = time.monotonic()
    ep = await pods.ensure(thread, cli)
    check(
        "ensure（建 Pod + 写 vault + 等 bridge 就绪）",
        ep.url.startswith("ws://"),
        f"{time.monotonic() - t0:.1f}s {ep.url}",
    )
    t1 = time.monotonic()
    again = await pods.ensure(thread, cli)
    check("复用", again == ep, f"{time.monotonic() - t1:.2f}s")

    # 握手：令牌 + 会话 id
    async with HostClient(
        ep.url + "/host", token=ep.token, session_id=tid, on_notification=noop, on_request=noop
    ) as c:
        check("bridge 握手（令牌来自 entrypoint 写的文件）", not c.closed)
    try:
        async with HostClient(
            ep.url + "/host", token="wrong", session_id=tid, on_notification=noop, on_request=noop
        ) as c:
            await asyncio.sleep(0.5)
            check("错误令牌被拒", c.closed, f"close_code={c.close_code}")
    except Exception as exc:
        check("错误令牌被拒", True, type(exc).__name__)

    route = await prov.routes.get(tid, fresh=True)
    sid = route.sandbox_id
    exec_url = await prov.lifecycle.endpoint(sid, 44772)
    execd = ExecdClient(exec_url, ep.token)
    out, _ = await run(
        execd,
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
        out, _ = await run(execd, git_cmd)
        check(
            "用 adapter 的环境 git ls-remote 私有仓库（Credential Vault）",
            "refs/heads" in out,
            out[:120],
        )
    else:
        print("跳过 git ls-remote（没给 PROBE_GIT_REPO）", flush=True)

    # 状态持久：写 /state → 让 Pod 失败 → 下一轮重建 → 还在
    await run(execd, "echo resume-me > /state/p0-marker")
    async with httpx.AsyncClient(timeout=30) as c:
        await c.delete(
            f"{prov.lifecycle.base_url}/v1/sandboxes/{sid}",
            headers={"OPEN-SANDBOX-API-KEY": settings.opensandbox_acp_api_key.get_secret_value()},
        )
    t2 = time.monotonic()
    ep2 = await pods.ensure(thread, cli)
    route2 = await prov.routes.get(tid, fresh=True)
    check("Pod 没了 → 重建", route2.sandbox_id != sid, f"{time.monotonic() - t2:.1f}s")
    execd2 = ExecdClient(await prov.lifecycle.endpoint(route2.sandbox_id, 44772), ep2.token)
    out, _ = await run(execd2, "cat /state/p0-marker")
    check("/state 跨 Pod 保留（CLI resume 的前提）", out == "resume-me", out)

    await prov.release(tid)
    check(
        "release", (await prov.lifecycle.list({"atlas.kind": "acp", "atlas.thread": tid})) == [], ""
    )
    await redis.aclose()
    failed = [n for n, ok in RESULTS if not ok]
    print(
        f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过"
        + (f"；失败：{failed}" if failed else ""),
        flush=True,
    )


asyncio.run(main())
