#!/usr/bin/env bash
# 部署后冒烟（doc/agent-platform-eval-plan.html W6）：demo 7 题 × 1 次，只看硬门槛。
#
#   deploy/local/smoke.sh      硬门槛不过时退出码非零
#
# ★ 需要 .env 里的 ATLAS_BENCH_UID（用户中心里的评测用户 atlas-bench）；没有就跳过（退出码 0）。
#   内网免令牌：评测带请求头 X-Uid 直连 port-forward 出来的 atlas-server（forward.sh）。
# ★ 会真实调用模型（约 0.2M tokens、3 分钟）。
# ★ 在「部署并滚动重启之后」跑（make smoke）。不挂在 apply.sh 末尾：apply.sh 只 apply 清单，
#   镜像标签不变时 Pod 不会更新 —— 那时测到的是旧版本，结论会误导。
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; . ./.env; set +a
if [ -z "${ATLAS_BENCH_UID:-}" ]; then
  echo "没有 ATLAS_BENCH_UID，跳过冒烟"
  exit 0
fi
# shellcheck source=forward.sh
. deploy/local/forward.sh
uv run python -m atlas_bench agent run agent-platform/bench/experiments/agent-l0.yaml --gate
