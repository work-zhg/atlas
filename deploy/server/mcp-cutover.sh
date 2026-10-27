#!/usr/bin/env bash
# MCP 切到自研 MCP 网关（doc/mcp-design.html 第二版 §12.4）的一次性步骤。可重复执行。
#
#   deploy/server/mcp-cutover.sh [--all-users]
#
# 前置（deploy.sh 与 register.sh 已经做完）：
#   · atlas-mcpgateway 在跑；Secret atlas-mcpgateway-creds 里有网关凭据
#   · Higress 上的 serpapi 只认网关凭据（deploy.sh 第 5 步：setup.py 带 MCP_GATEWAY_CREDENTIAL）
#   · register.sh 已把 MCP 网关登记到用户中心（应用 mcp-gateway、数据编码 McpServer）；Atlas 名下的
#     mcp:manage、AtlasMcpServer 已由 Atlas 的 register-uc 退役（持有人、授权搬到了网关）
#   · atlas-server 只经 MCP 网关调 MCP（30-server.yaml 的 MCP_GATEWAY_URL）
#
# 做三件事（都在 atlas-mcpgateway 的 Pod 里跑，注册人 / 操作人 = 用户中心的 admin）：
#   1. 登记原来经 MCP_SERVERS 接入的服务（serpapi）：接入检查要通过 —— Higress 没改成只认网关凭据
#      的话这里会失败，这正是要的
#   2. --all-users：给根部门（含下级）授「使用」，保持切换前「谁都能用」。★ 不给就只有所有者
#      （admin）能用，其他人要逐个授权 —— 这是方案的本意（§1.1「谁都能用」是要解决的问题）
#   3. 给有授权但没有 Key 的人批量发 Key（以 Agent 为权限的 Agent，它的数字员工要另外授权、发 Key）
#
# ★ AtlasMcpServer 下如果有单条数据而网关里还没有对应的服务，Atlas 的 register-uc 会先保留它：
#   本脚本登记完服务后，再跑一次 register.sh 即可删掉。
set -euo pipefail
cd "$(dirname "$0")/../.."

SERVER_IP="${ATLAS_SERVER_IP:-192.168.0.112}"
export KUBECONFIG="${ATLAS_KUBECONFIG:-$HOME/.kube/atlas-${SERVER_IP}.yaml}"
ALL_USERS="${1:-}"

# 原来 atlas-server 的 MCP_SERVERS（切到 MCP 网关之前 30-server.yaml 里的值）。地址只用来认出 Higress 上的名字
LEGACY_MCP_SERVERS='[{"name":"serpapi","url":"http://higress/mcp-servers/serpapi","call_timeout_s":90}]'

gw() { kubectl -n atlas-system exec -i deploy/atlas-mcpgateway -- python -m atlas_mcpgateway "$@"; }

echo "── 1. 登记服务"
gw import --owner-account admin --from-env "$LEGACY_MCP_SERVERS"

if [ "$ALL_USERS" = "--all-users" ]; then
  echo "── 2. 全员可用（根部门含下级 · 使用）"
  gw grant serpapi --all-users --actor-account admin
else
  echo "── 2. 跳过全员授权：只有所有者能用，其他人在 MCP 管理端逐个授权（或加 --all-users 重跑）"
fi

echo "── 3. 给有授权但没有 Key 的人发 Key"
gw key issue-missing --actor-account admin

echo "✓ 完成。MCP 管理端：http://atlas.${SERVER_IP}.nip.io/atlas/mcp"
