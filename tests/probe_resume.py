"""诊断工具：在 Pod 里用指定 HOME 试一次 session/load。

配合 test_acp_real_cli.py 里那条 xfail 用 —— 它能把"是不是 FUSE 的问题"
和"是不是内容的问题"分开：同一个 Pod 里换个 HOME 跑一遍就知道。

不是测试，pytest 不会收集它（文件名不以 test_ 开头）。

    python tests/_probe_resume.py <HOME> <sessionId>
"""
import asyncio, json, os, sys

async def main():
    home, sid = sys.argv[1], sys.argv[2]
    env = dict(os.environ); env["HOME"] = home
    p = await asyncio.create_subprocess_exec(
        "/opt/node/bin/node",
        "/opt/acp-cli/node_modules/@zed-industries/claude-code-acp/dist/index.js",
        stdin=-1, stdout=-1, stderr=-1, cwd="/workspace", env=env)
    n = [0]
    async def send(m, par):
        n[0] += 1
        "/opt/acp-cli/node_modules/@zed-industries/claude-code-acp/dist/index.js",
        await p.stdin.drain(); return n[0]
    async def wait(i, budget=90):
        try:
            async with asyncio.timeout(budget):
                while True:
                    line = await p.stdout.readline()
                    if not line: return None
                    try: f = json.loads(line)
                    except Exception: continue
                    if f.get("id") == i: return f
        except TimeoutError: return "TIMEOUT"
    await wait(await send("initialize", {"protocolVersion":1,"clientCapabilities":{"fs":{}}}))
    r = await wait(await send("session/load", {"sessionId":sid,"cwd":"/workspace","mcpServers":[]}))
    ok = isinstance(r, dict) and "result" in r
    print(f"HOME={home} -> session/load {'成功' if ok else '失败'}")
    if not ok:
        print("  返回:", json.dumps(r, ensure_ascii=False)[:200] if isinstance(r,dict) else r)
        import contextlib
        with contextlib.suppress(Exception):
            async with asyncio.timeout(5):
                err = await p.stderr.read(8000)
            print("  === adapter stderr ===")
            print(err.decode("utf-8","replace")[-3000:])
    import contextlib
    with contextlib.suppress(Exception):
        p.kill()

asyncio.run(main())
