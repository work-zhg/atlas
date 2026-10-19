#!/usr/bin/env bash
# L1 回归（doc/agent-platform-eval-plan.html §3 / §8.2）：跑一轮 agent-baseline，与基线按题配对比较。
#
#   deploy/local/regress.sh          回归或硬门槛不过时退出码非零
#   BASELINE=<运行目录> deploy/local/regress.sh
#
# 基线默认取 var/bench/baselines/agent-l1（指向某次运行目录的链接）。只在确认改进或有意改变
# 行为时更新它：ln -sfn <新运行目录> var/bench/baselines/agent-l1
# ★ 会真实调用模型（约 50 个 trial，十几分钟）。
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; . ./.env; set +a
BASELINE="${BASELINE:-var/bench/baselines/agent-l1}"
[ -e "$BASELINE" ] || { echo "没有基线 $BASELINE"; exit 1; }
[ -n "${ATLAS_BENCH_UID:-}" ] || { echo "缺 ATLAS_BENCH_UID（用户中心里的评测用户 atlas-bench）"; exit 1; }
# 内网免令牌：评测带请求头 X-Uid 直连 port-forward 出来的 atlas-server（forward.sh）
# shellcheck source=forward.sh
. deploy/local/forward.sh
out="$(mktemp)"
status=0
uv run python -m atlas_bench agent run agent-platform/bench/experiments/agent-baseline.yaml --gate | tee "$out" || status=$?
run_dir="$(sed -n 's/^运行目录：//p' "$out")"
rm -f "$out"
[ -n "$run_dir" ] || { echo "没拿到运行目录"; exit 1; }
uv run python -m atlas_bench agent compare "$BASELINE" "$run_dir" || status=$?
exit "$status"
