#!/usr/bin/env bash
# 部署后的一次性登记：把 MCP 网关、Atlas、TeamFlow、记忆服务登记到用户中心（应用编码、操作码、菜单、
# 角色、数据编码）。不做这一步，它们调用户中心的开放接口会 403「应用 … 不存在或已停用」。
# ★ 记忆服务的数据编码按它库里打开了数据权限开关的作用域生成，所以在集群里的 Pod 中执行（读得到库）；
#   新打开某个作用域的开关之后要再跑一次。
#
#   deploy/server/register.sh
#
# 前置：超级管理员 admin 已在 http://atlas.<服务器>.nip.io/uc/login 用临时密码登录、改过密码
#   （register-uc 拒绝临时密码）。admin 的密码：环境变量 UC_ADMIN_PASSWORD 有就用，没有就提示输入；
#   只在本进程的环境里，不落盘。
# ★ 用户中心不对外（ClusterIP）：经 port-forward 访问。可重复执行。
set -euo pipefail
cd "$(dirname "$0")/../.."

SERVER_IP="${ATLAS_SERVER_IP:-192.168.0.112}"
export NO_PROXY="${NO_PROXY:+${NO_PROXY},}${SERVER_IP},127.0.0.1" no_proxy="${no_proxy:+${no_proxy},}${SERVER_IP},127.0.0.1"
export KUBECONFIG="${ATLAS_KUBECONFIG:-$HOME/.kube/atlas-${SERVER_IP}.yaml}"

if [ -z "${UC_ADMIN_PASSWORD:-}" ]; then
  read -r -s -p "用户中心 admin 的密码：" UC_ADMIN_PASSWORD
  echo
fi
export UC_ADMIN_PASSWORD

kubectl -n atlas-system port-forward svc/atlas-usercenter 28030:8030 >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null || true' EXIT
for _ in $(seq 50); do (echo >/dev/tcp/127.0.0.1/28030) 2>/dev/null && break; sleep 0.2; done

UC=http://127.0.0.1:28030
# ★ 密码经 stdin 传进 Pod，不出现在命令行参数里（本机与 Pod 的进程列表都看不到）
in_pod() {
  printf '%s\n' "$UC_ADMIN_PASSWORD" | kubectl -n atlas-system exec -i "deploy/$1" -- sh -c \
    "read -r UC_ADMIN_PASSWORD && export UC_ADMIN_PASSWORD && $2"
}
# ★ MCP 网关要在 Atlas 之前：Atlas 退役它名下的 mcp:manage、AtlasMcpServer 时，把持有人和授权搬到
#   网关的 MCP_ADMIN、McpServer（网关没登记时 Atlas 先保留它们，再跑一次本脚本即可）
in_pod atlas-mcpgateway "python -m atlas_mcpgateway register-uc --account admin"
uv run python -m atlas_agent register-uc --account admin --uc-url "$UC"
uv run python -m atlas_teamflow register-uc --account admin --uc-url "$UC"
in_pod atlas-memory "python -m atlas_memory register-uc --account admin"
echo "✓ 已登记。用 admin 登录 http://atlas.${SERVER_IP}.nip.io/atlas 即可使用"
