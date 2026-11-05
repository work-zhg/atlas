"""原生沙箱对真 OpenSandbox 的实测（doc/sandbox-opensandbox-design.html §20.1 的 P0-5、P0-8）。

用 Atlas 的沙箱组件（sandbox/component.py：基座 → 通用供给 → 原生沙箱 → 命令执行）在集群内跑：
沙箱 Pod 的 IP 只在集群网络里可达。由 opensandbox_probe.sh 起一个一次性 Pod 执行。
★ 沙箱先登记再建：要连 DATABASE_URL 指向的库（迁移到最新，模板种子在里面）。

覆盖：建 / 列 / 过滤 / 不过期；strict 档（UID 1000、execd 作 PID 1、根只读、没有凭据和 SA 令牌）；
后台命令的游标与状态、中断杀整个进程组、未知 id；文件会话落在 CSI 挂载上；嵌套目录 rename
（s3fs 1.96 / 1.97 的回归，升级 s3fs 后必跑）；ShellSandbox 的重放与取消；
--restart-server 时验证 server 重启期间命令不受影响；alpine 镜像注入；release。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from typing import Any
from uuid import UUID

import httpx
from atlas_agent.config import get_settings
from atlas_agent.domain.spec import SandboxSpec
from atlas_agent.ids import uuid7
from atlas_agent.redisx import make_redis
from atlas_agent.sandbox.base.execution.values import CommandGone, CommandSpec
from atlas_agent.sandbox.base.lifecycle.runtime import EXECD_PORT
from atlas_agent.sandbox.component import SandboxComponent
from atlas_agent.sandbox.exec import ShellSandbox
from atlas_agent.sandbox.native import WorkspaceRef

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


class Exec:
    """一个沙箱上的命令：后台提交、按游标跟到退出。"""

    def __init__(self, c: SandboxComponent, sandbox_uuid: UUID) -> None:
        self.exec, self.sid = c.exec, sandbox_uuid

    async def start(self, cmd: str) -> str:
        return await self.exec.start(self.sid, CommandSpec(cmd, cwd="/workspace"))

    async def run(self, cmd: str, timeout: float = 60) -> tuple[str, int | None]:
        cmd_id = await self.start(cmd)
        out, cursor = b"", 0
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = await self.exec.read(self.sid, cmd_id, cursor)
            out, cursor = out + chunk.data, chunk.cursor
            st = await self.exec.status(self.sid, cmd_id)
            if not st.running:
                chunk = await self.exec.read(self.sid, cmd_id, cursor)
                return (out + chunk.data).decode(errors="replace"), st.exit_code
            await asyncio.sleep(0.2)
        return out.decode(errors="replace"), None


async def lifecycle(settings: Any, method: str, path: str) -> Any:
    """直接问 opensandbox-sbx（看 Atlas 的客户端不交出的原样字段：expiresAt、metadata）。"""
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        resp = await client.request(
            method,
            f"{str(settings.opensandbox_sbx_url).rstrip('/')}/v1{path}",
            headers={"OPEN-SANDBOX-API-KEY": settings.opensandbox_sbx_api_key.get_secret_value()},
        )
    return resp.json() if resp.status_code < 400 else None


async def main() -> None:
    settings = get_settings()
    redis = make_redis(settings)
    c = SandboxComponent.build(settings, redis)
    ws = WorkspaceRef(uuid7(), uuid7())
    print(f"workspace={ws.root_thread_uuid}", flush=True)

    # ── P0-8 创建：timeout null、标签、端点 ──
    t0 = time.monotonic()
    ref = await c.native.ensure(ws, SandboxSpec())
    instance = await c.runtime.get(ref.sandbox_uuid)
    assert instance is not None and instance.external_id
    access = await c.runtime.access(ref.sandbox_uuid)
    check("ensure 建沙箱", True, f"{instance.external_id} {time.monotonic() - t0:.1f}s")
    sb = await lifecycle(settings, "GET", f"/sandboxes/{instance.external_id}")
    check("timeout null → 没有 expiresAt", not sb.get("expiresAt"), json.dumps(sb.get("expiresAt")))
    check(
        "标签原样返回（人、sandbox_uuid）",
        (sb.get("metadata") or {}).get("atlas.sandbox") == str(ref.sandbox_uuid),
        json.dumps(sb.get("metadata")),
    )
    q = f"atlas.sandbox%3D{ref.sandbox_uuid}"
    listed = (await lifecycle(settings, "GET", f"/sandboxes?metadata={q}"))["items"]
    check(
        "GET /sandboxes?metadata= 过滤",
        [x["id"] for x in listed] == [instance.external_id],
        str([x["id"] for x in listed]),
    )
    other = (await lifecycle(settings, "GET", f"/sandboxes?metadata=atlas.sandbox%3D{uuid7()}"))[
        "items"
    ]
    check("过滤不到时为空", other == [], str(other))
    check("端点是 Pod IP:44772", access.url.endswith(f":{EXECD_PORT}"), access.url)
    again = await c.native.ensure(ws, SandboxSpec())
    check("再次 ensure 复用同一个", again == ref, str(again.sandbox_uuid))

    execd = Exec(c, ref.sandbox_uuid)
    # ── P0-5 strict 档 ──
    out, code = await execd.run(
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
    out, _ = await execd.run(
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
            await httpx.AsyncClient(trust_env=False).get(
                f"{access.url}/command/status/x", headers={"X-EXECD-ACCESS-TOKEN": "bad"}
            )
        ).status_code
        in (401, 403),
        "",
    )
    out, _ = await execd.run(
        "env | grep -c -E 'OSS|MINIO|SECRET|ACCESS_KEY' || true;"
        " ls /var/run/secrets/kubernetes.io 2>&1 | head -1",
    )
    check(
        "沙箱里没有对象存储凭据 / SA 令牌",
        out.splitlines()[0].strip() == "0" and "No such file" in out,
        out.replace("\n", " | "),
    )

    # ── 后台命令：游标、状态、中断杀进程组 ──
    sid = ref.sandbox_uuid
    cmd_id = await execd.start("for i in 1 2 3; do echo line$i; sleep 0.5; done")
    await asyncio.sleep(0.8)
    chunk1 = await c.exec.read(sid, cmd_id, 0)
    await asyncio.sleep(1.5)
    chunk2 = await c.exec.read(sid, cmd_id, chunk1.cursor)
    check(
        "游标是字节偏移、续读不重不漏",
        (chunk1.data + chunk2.data).decode() == "line1\nline2\nline3\n"
        and chunk1.cursor == len(chunk1.data),
        f"c1={chunk1.cursor} c2={chunk2.cursor} {chunk1.data!r}+{chunk2.data!r}",
    )
    st = await c.exec.status(sid, cmd_id)
    check("status 有 exit_code", st.running is False and st.exit_code == 0, str(st))
    cmd_id = await execd.start("sh -c 'sleep 300 & sleep 300 & wait' & sleep 300; wait")
    await asyncio.sleep(1)
    await c.exec.interrupt(sid, cmd_id)
    await asyncio.sleep(3)
    out, _ = await execd.run("ps -eo args | grep -c '[s]leep 300' || true")
    check("中断杀整个进程组（含孙进程）", out.strip() == "0", f"剩 {out.strip()} 个 sleep")
    for name, call in (
        ("未知 id → status 404", c.exec.status(sid, "no-such-id")),
        ("未知 id → logs 400/404", c.exec.read(sid, "no-such-id", 0)),
    ):
        try:
            await call
            check(name, False, "")
        except CommandGone:
            check(name, True, "CommandGone")
        except Exception as exc:
            check(name, False, type(exc).__name__)

    # ── 文件 API 落在 CSI 挂载上 ──
    files = await c.exec.open_files(sid)
    files.upload("/workspace/p0/up.txt", b"uploaded")
    out, _ = await execd.run("cat /workspace/p0/up.txt; echo; stat -c '%u %a' /workspace/p0/up.txt")
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
    await execd.run(
        "mkdir -p /workspace/p0/empty /workspace/p0/sub && echo s > /workspace/p0/sub/s.txt"
        " && ln -sf up.txt /workspace/p0/link.txt",
    )
    found = sorted(f.path for f in files.search("/workspace/p0"))
    check(
        "search 递归列文件（不含空目录）",
        found[:3] == ["/workspace/p0/link.txt", "/workspace/p0/sub/s.txt", "/workspace/p0/up.txt"]
        or "/workspace/p0/sub/s.txt" in found,
        str(found),
    )
    check("search 不存在的目录 → []", files.search("/workspace/no-such-dir") == [], "")
    files.remove(["/workspace/p0/sub/s.txt"])
    out, _ = await execd.run("test -e /workspace/p0/sub/s.txt && echo still || echo gone")
    check("remove", out.strip() == "gone", out.strip())
    files.close()

    # ── 嵌套目录 rename：s3fs 1.96 / 1.97 会做一半就报 ENOTEMPTY（上游 d400a5c 修复） ──
    out, code = await execd.run(
        "cd /workspace && mkdir -p p0/k1/s && echo q > p0/k1/s/q && mv p0/k1 p0/k2"
        " && ls -R p0/k2 && test ! -e p0/k1",
    )
    check("只含子目录的目录 rename", code == 0, out.replace("\n", " | "))

    # ── ShellSandbox 端到端 ──
    sbx = ShellSandbox(
        native=c.native,
        exec=c.exec,
        ledger=c.ledger,
        ws=ws,
        run_id=f"r-{ws.root_thread_uuid}",
        spec=SandboxSpec(),
    )
    r = await sbx.aexecute("echo hi > e2e.txt && cat e2e.txt", exec_id="c1")
    check("ShellSandbox.aexecute", r.output == "hi\n" and r.exit_code == 0, repr(r.output))
    r2 = await sbx.aexecute("echo hi >> e2e.txt && cat e2e.txt", exec_id="c1")
    check("重放同一个 tool_call 不重跑", r2.output == "hi\n", repr(r2.output))
    task = asyncio.create_task(sbx.aexecute("sleep 300", exec_id="c2"))
    await asyncio.sleep(1.5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await asyncio.sleep(2)
    out, _ = await execd.run("ps -eo args | grep -c '[s]leep 300' || true")
    check("取消 → 命令被中断", out.strip() == "0", f"剩 {out.strip()}")

    # ── P0-8 server 重启期间运行中的命令不受影响 ──
    if "--restart-server" in sys.argv:
        cmd_id = await execd.start("for i in $(seq 40); do echo t$i; sleep 1; done")
        print("现在重启 opensandbox-sbx（脚本等 40 秒）", flush=True)
        await asyncio.sleep(40)
        out, _ = await execd.run("true")
        text = (await c.exec.read(sid, cmd_id, 0)).data.decode()
        check("server 重启期间命令跑完", text.count("\n") == 40, f"{text.count(chr(10))} 行")
        check("重启后还在", await c.runtime.get(sid) is not None, "")

    # ── alpine 镜像也能当沙箱 ──
    ws2 = WorkspaceRef(uuid7(), ws.user_uuid)
    c2 = SandboxComponent.build(
        settings.model_copy(update={"sandbox_image_allow_prefixes": ["alpine:"]}), redis
    )
    ref2 = await c2.native.ensure(ws2, SandboxSpec(image="alpine:3"))
    out, code = await Exec(c2, ref2.sandbox_uuid).run(
        "cat /etc/os-release | head -1; id -u; echo x > /workspace/a && cat /workspace/a"
    )
    check("注入 alpine 镜像", "Alpine" in out and code == 0, out.replace("\n", " | "))

    # ── 清理 ──
    if "--keep" not in sys.argv:
        await c.native.release(ws)
        await c2.native.release(ws2)
        await asyncio.sleep(2)
        gone = await lifecycle(settings, "GET", f"/sandboxes/{instance.external_id}")
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
