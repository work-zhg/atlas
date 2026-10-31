#!/usr/bin/env bash
# 按 uv.lock 导出 app 镜像的第三方依赖：docker/app/requirements.txt（docker/app/Dockerfile 先装它，再拷源码）。
#
#   docker/app/requirements.sh
#
# ★ 只导出镜像里装的那几个服务的依赖闭包，与 Dockerfile 里 pip install 的本仓包一致；
#   用 --all-packages 会把评测的依赖（datasets、matplotlib 等）也装进镜像。
# ★ --frozen：只读锁文件，不重新解析；锁文件与 pyproject 不同步时先 uv lock，而不是在这里悄悄改版本。
set -euo pipefail
cd "$(dirname "$0")/../.."

uv export --frozen --no-dev --no-emit-workspace --no-hashes --no-header --no-annotate --quiet \
  --package atlas-agent --package atlas-skill --package atlas-knowledge --package atlas-memory \
  --package atlas-mcpgateway --package atlas-modelgateway --package atlas-usercenter --package atlas-teamflow \
  -o docker/app/requirements.txt
