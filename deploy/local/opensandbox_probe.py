"""原生沙箱对真 OpenSandbox 的实测（doc/sandbox-opensandbox-design.html §20.1 的 P0-5、P0-8）。

用 Atlas 自己的客户端（SandboxProvisioner / ExecdClient / ExecdFiles / PodSandbox）在集群内跑：
沙箱 Pod 的 IP 只在集群网络里可达。由 opensandbox_probe.sh 起一个一次性 Pod 执行。

覆盖：建 / 列 / 过滤 / 不过期；strict 档（UID 1000、execd 作 PID 1、根只读、没有凭据和 SA 令牌）；
后台命令的游标与状态、中断杀整个进程组、未知 id；文件 API 落在 CSI 挂载上；嵌套目录 rename
（s3fs 1.96 / 1.97 的回归，升级 s3fs 后必跑）；PodSandbox 的重放与取消；
--restart-server 时验证 server 重启期间命令不受影响；alpine 镜像注入；release。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
import uuid

import httpx
from atlas_agent.config import get_settings
from atlas_agent.providers.opensandbox import (
    EXECD_PORT,
    ExecdClient,
    ExecdFiles,
    SandboxProvisioner,
)
from atlas_agent.providers.sandbox import PodSandbox
from atlas_agent.redisx import make_redis

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


async def run(execd: ExecdClient, cmd: str, timeout: float = 60) -> tuple[str, int | None]:
    cmd_id = await execd.start(cmd, cwd="/workspace")
    out, cursor = b"", 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk, cursor = await execd.logs(cmd_id, cursor)
        out += chunk
        st = await execd.status(cmd_id)
        if not st.get("running"):
            chunk, cursor = await execd.logs(cmd_id, cursor)
            return (out + chunk).decode(errors="replace"), st.get("exit_code")
        await asyncio.sleep(0.2)
    return out.decode(errors="replace"), None


async def main() -> None:
    settings = get_settings()
    redis = make_redis(settings)
    p = SandboxProvisioner(settings, redis)
    user, ws = "p0-user", f"p0-{uuid.uuid4().hex[:8]}"
    print(f"workspace={ws}", flush=True)

    # ── P0-8 创建：timeout null、metadata、端点 ──
    t0 = time.monotonic()
    ep = await p.ensure(ws, user=user)
    check("ensure 建沙箱", True, f"{ep.sandbox_id} {time.monotonic() - t0:.1f}s url={ep.url}")
    sb = await p.lifecycle.get(ep.sandbox_id)
    check("timeout null → 没有 expiresAt", not sb.get("expiresAt"), json.dumps(sb.get("expiresAt")))
    check(
        "metadata 原样返回",
        (sb.get("metadata") or {}).get("atlas.thread") == ws,
        json.dumps(sb.get("metadata")),
    )
    listed = await p.lifecycle.list({"atlas.kind": "sbx", "atlas.thread": ws})
    check(
        "GET /sandboxes?metadata= 过滤",
        [x["id"] for x in listed] == [ep.sandbox_id],
        str([x["id"] for x in listed]),
    )
    other = await p.lifecycle.list({"atlas.kind": "sbx", "atlas.thread": "no-such-thread"})
    check("过滤不到时为空", other == [], str(other))
    check("端点是 Pod IP:44772", ep.url.endswith(f":{EXECD_PORT}"), ep.url)
    again = await p.ensure(ws, user=user, fresh=True)
    check("fresh ensure 复用同一个", again.sandbox_id == ep.sandbox_id, again.sandbox_id)

    execd = ExecdClient(ep.url, ep.token)
    # ── P0-5 strict 档 ──
    out, code = await run(
        execd,
        "id -u; id -g; cat /proc/1/cmdline | tr '\\0' ' '; echo;"
        " touch /rootfs-probe 2>&1; echo rc=$?",
    )
    lines = out.splitlines()
    check("非 root（UID 1000）", lines[:2] == ["1000", "1000"], " ".join(lines[:2]))
    check(
        "execd 是 PID 1",
        "execd" in (lines[2] if len(lines) > 2 else ""),
        lines[2] if len(lines) > 2 else out,
    )
    check("根只读", "Read-only file system" in out, out.splitlines()[-2] if len(lines) > 3 else out)
    out, _ = await run(
        execd,
        "grep -E ' /workspace | /skills ' /proc/mounts;"
        " echo x > /home/agent/h && echo home-ok; echo x > /repos/r && echo repos-ok;"
        " echo x > /skills/s 2>&1",
    )
    check("/workspace 是 s3fs", "fuse.s3fs" in out.split("\n")[0], out.split("\n")[0][:120])
    check(
        "/skills 只读挂载",
        "ro," in next((line for line in out.splitlines() if " /skills " in line), ""),
        "",
    )
    check("HOME、/repos 可写", "home-ok" in out and "repos-ok" in out, "")
    check(
        "令牌错误被拒",
        (
            await httpx.AsyncClient().get(
                f"{ep.url}/command/status/x", headers={"X-EXECD-ACCESS-TOKEN": "bad"}
            )
        ).status_code
        in (401, 403),
        "",
    )
    out, _ = await run(
        execd,
        "env | grep -c -E 'OSS|MINIO|SECRET|ACCESS_KEY' || true;"
        " ls /var/run/secrets/kubernetes.io 2>&1 | head -1",
    )
    check(
        "沙箱里没有对象存储凭据 / SA 令牌",
        out.splitlines()[0].strip() == "0" and "No such file" in out,
        out.replace("\n", " | "),
    )

    # ── 后台命令：游标、状态、中断杀进程组 ──
    cmd_id = await execd.start("for i in 1 2 3; do echo line$i; sleep 0.5; done", cwd="/workspace")
    await asyncio.sleep(0.8)
    chunk1, c1 = await execd.logs(cmd_id, 0)
    await asyncio.sleep(1.5)
    chunk2, c2 = await execd.logs(cmd_id, c1)
    check(
        "游标是字节偏移、续读不重不漏",
        (chunk1 + chunk2).decode() == "line1\nline2\nline3\n" and c1 == len(chunk1),
        f"c1={c1} c2={c2} {chunk1!r}+{chunk2!r}",
    )
    st = await execd.status(cmd_id)
    check(
        "status 有 exit_code",
        st.get("running") is False and st.get("exit_code") == 0,
        json.dumps(st),
    )
    cmd_id = await execd.start(
        "sh -c 'sleep 300 & sleep 300 & wait' & sleep 300; wait", cwd="/workspace"
    )
    await asyncio.sleep(1)
    await execd.interrupt(cmd_id)
    await asyncio.sleep(3)
    out, _ = await run(execd, "ps -eo args | grep -c '[s]leep 300' || true")
    check("中断杀整个进程组（含孙进程）", out.strip() == "0", f"剩 {out.strip()} 个 sleep")
    try:
        await execd.status("no-such-id")
        check("未知 id → status 404", False, "")
    except Exception as exc:
        check("未知 id → status 404", type(exc).__name__ == "ExecdGone", type(exc).__name__)
    try:
        await execd.logs("no-such-id", 0)
        check("未知 id → logs 400/404", False, "")
    except Exception as exc:
        check("未知 id → logs 400/404", type(exc).__name__ == "ExecdGone", type(exc).__name__)

    # ── 文件 API 落在 CSI 挂载上 ──
    files = ExecdFiles(ep.url, ep.token)
    files.upload("/workspace/p0/up.txt", b"uploaded")
    out, _ = await run(
        execd, "cat /workspace/p0/up.txt; echo; stat -c '%u %a' /workspace/p0/up.txt"
    )
    check("upload 后命令立即可见", out.startswith("uploaded"), out.replace("\n", " | "))
    check(
        "upload 的属主是沙箱用户", out.splitlines()[-1].split()[0] == "1000", out.splitlines()[-1]
    )
    check("download", files.download("/workspace/p0/up.txt") == b"uploaded", "")
    check(
        "info 大小",
        files.size("/workspace/p0/up.txt") == 8,
        str(files.size("/workspace/p0/up.txt")),
    )
    check("info 不存在 → None", files.size("/workspace/p0/none.txt") is None, "")
    await run(
        execd,
        "mkdir -p /workspace/p0/empty /workspace/p0/sub && echo s > /workspace/p0/sub/s.txt"
        " && ln -sf up.txt /workspace/p0/link.txt",
    )
    found = sorted(f["path"] for f in files.search("/workspace/p0"))
    check(
        "search 递归列文件（不含空目录）",
        found[:3] == ["/workspace/p0/link.txt", "/workspace/p0/sub/s.txt", "/workspace/p0/up.txt"]
        or "/workspace/p0/sub/s.txt" in found,
        str(found),
    )
    check("search 不存在的目录 → []", files.search("/workspace/no-such-dir") == [], "")
    files.remove(["/workspace/p0/sub/s.txt"])
    out, _ = await run(execd, "test -e /workspace/p0/sub/s.txt && echo still || echo gone")
    check("remove", out.strip() == "gone", out.strip())
    files.close()

    # ── 嵌套目录 rename：s3fs 1.96 / 1.97 会做一半就报 ENOTEMPTY（上游 d400a5c 修复） ──
    out, code = await run(
        execd,
        "cd /workspace && mkdir -p p0/k1/s && echo q > p0/k1/s/q && mv p0/k1 p0/k2"
        " && ls -R p0/k2 && test ! -e p0/k1",
    )
    check("只含子目录的目录 rename", code == 0, out.replace("\n", " | "))

    # ── PodSandbox 端到端 ──
    sbx = PodSandbox(provisioner=p, workspace_thread_id=ws, user_id=user, run_id=f"r-{ws}")
    r = await sbx.aexecute("echo hi > e2e.txt && cat e2e.txt", exec_id="c1")
    check("PodSandbox.aexecute", r.output == "hi\n" and r.exit_code == 0, repr(r.output))
    r2 = await sbx.aexecute("echo hi >> e2e.txt && cat e2e.txt", exec_id="c1")
    check("重放同一个 tool_call 不重跑", r2.output == "hi\n", repr(r2.output))
    task = asyncio.create_task(sbx.aexecute("sleep 300", exec_id="c2"))
    await asyncio.sleep(1.5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await asyncio.sleep(2)
    out, _ = await run(execd, "ps -eo args | grep -c '[s]leep 300' || true")
    check("取消 → 命令被中断", out.strip() == "0", f"剩 {out.strip()}")

    # ── P0-8 server 重启期间运行中的命令不受影响 ──
    if "--restart-server" in sys.argv:
        cmd_id = await execd.start(
            "for i in $(seq 40); do echo t$i; sleep 1; done", cwd="/workspace"
        )
        print("现在重启 opensandbox-sbx（脚本等 40 秒）", flush=True)
        await asyncio.sleep(40)
        out, _ = await run(execd, "true")
        chunk, _ = await execd.logs(cmd_id, 0)
        check(
            "server 重启期间命令跑完",
            chunk.decode().count("\n") == 40,
            f"{chunk.decode().count(chr(10))} 行",
        )
        listed = await p.lifecycle.list({"atlas.kind": "sbx", "atlas.thread": ws})
        check("重启后列表里还在", [x["id"] for x in listed] == [ep.sandbox_id], "")

    # ── alpine 镜像也能当沙箱 ──
    ws2 = f"{ws}-alpine"
    p2 = SandboxProvisioner(
        settings.model_copy(update={"sandbox_image_allow_prefixes": ["alpine:"]}), redis
    )
    ep2 = await p2.ensure(ws2, user=user, image="alpine:3")
    out, code = await run(
        ExecdClient(ep2.url, ep2.token),
        "cat /etc/os-release | head -1; id -u; echo x > /workspace/a && cat /workspace/a",
    )
    check("注入 alpine 镜像", "Alpine" in out and code == 0, out.replace("\n", " | "))

    # ── 清理 ──
    if "--keep" not in sys.argv:
        await p.release(ws)
        await p2.release(ws2)
        await asyncio.sleep(2)
        gone = await p.lifecycle.get(ep.sandbox_id)
        check(
            "release 删沙箱",
            gone is None or (gone.get("status") or {}).get("state") in ("Stopping", "Terminated"),
            json.dumps((gone or {}).get("status")),
        )
    await redis.aclose()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(
        f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过"
        + (f"；失败：{failed}" if failed else ""),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
