"""python -m atlas_gateway --check | --out DIR

--check                 只校验硬规则（CI）；不查证书
--hosts                 列出各站点的域名（一行一个；deploy/server/gateway.sh 申请证书用）
--out DIR               校验后渲染到 DIR（部署：deploy/server/gateway.sh）
--install-dir DIR       配置最终放在哪（Lua 路径、include 用绝对路径）；默认同 --out
--allow-missing-certs   证书还没签好的站点只生成 80（给 certbot 验证用），不算违规
--env KEY=VALUE         覆盖环境变量（routes.yaml 里的 ${KEY}），可重复
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .model import ConfigError, load
from .paths import plan
from .render import render
from .rules import check

DEFAULT_ROUTES = Path(__file__).resolve().parents[2] / "routes.yaml"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas_gateway", description="校验并渲染 API 网关的配置")
    ap.add_argument("--routes", type=Path, default=DEFAULT_ROUTES)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--hosts", action="store_true")
    mode.add_argument("--out", type=Path)
    ap.add_argument("--install-dir")
    ap.add_argument("--allow-missing-certs", action="store_true")
    ap.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    args = ap.parse_args(argv)

    env = dict(os.environ)
    for kv in args.env:
        key, sep, value = kv.partition("=")
        if not sep:
            ap.error(f"--env 要写成 KEY=VALUE：{kv}")
        env[key] = value
    try:
        table = load(args.routes, env)
    except ConfigError as exc:
        print(f"routes.yaml 读不进来：{exc}", file=sys.stderr)
        return 2

    if args.hosts:
        print("\n".join(s.host for s in table.sites))
        return 0
    cert_root = None if args.check else Path(table.settings.cert_dir)
    problems = check(table, cert_root=cert_root)
    tls_hosts: set[str] | None = None
    if args.allow_missing_certs and cert_root is not None:
        missing = {p.missing_cert_host for p in problems if p.missing_cert_host}
        problems = [p for p in problems if not p.missing_cert_host]
        tls_hosts = {s.host for s in table.sites} - missing
        for host in sorted(missing):
            print(f"注意：{host} 还没有证书，只生成 80 端口（ACME 验证）", file=sys.stderr)
    if problems:
        print("硬规则不通过，不生成配置：", file=sys.stderr)
        for p in problems:
            print(f"  · {p}", file=sys.stderr)
        return 1

    locs = plan(table)
    if args.check:
        print(
            f"通过：{len(table.sites)} 个站点，{len(table.routes())} 条路由，"
            f"{len(locs)} 个 location"
        )
        return 0
    files = render(
        table,
        args.out,
        install_dir=args.install_dir or str(args.out.resolve()),
        tls_hosts=tls_hosts,
    )
    print(f"已生成 {len(files)} 个文件到 {args.out}（{len(locs)} 个 location）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
