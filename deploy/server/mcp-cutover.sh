#!/usr/bin/env bash
# MCP 网关改为直连上游（原型 agent-platform/doc/prototype/mcp-gateway.html）之后的一次性步骤。可重复执行。
#
#   deploy/server/mcp-cutover.sh
#
# 前置（deploy.sh 与 register.sh 已经做完）：
#   · atlas-mcpgateway 已是新版本：迁移 g0002 重建了网关的表 —— 原来登记的服务、Key、复核结论都清掉了
#   · register.sh 已把 MCP 网关登记到用户中心（数据编码 McpServer、McpTool；「全部 MCP 服务 · 所有者」
#     同步为「全部 MCP 工具 · 所有者」）
#   · 仓库根 .env 有 SERPAPI_KEY
#
# 做一件事：把 serpapi 登记成 REST 转换（deploy/server/mcp-serpapi.yaml，直连 serpapi.com，私有），
# 登记人 / 所有者 = 用户中心的 admin。已存在就跳过。
#
# ★ 之后在 MCP 管理端「API Key · 智能体 Key」里给要用它的智能体分配 serpapi（智能体的 Key 由 Atlas
#   在编辑、运行智能体时自动申请）。也可以在服务详情里把它改成公开：任何 Key 都能用。
# ★ Higress 不再在 MCP 的链路上：上面的 serpapi MCP Server、消费者 mcp-gateway 可以在控制台删掉。
set -euo pipefail
cd "$(dirname "$0")/../.."

SERVER_IP="${ATLAS_SERVER_IP:-192.168.0.112}"
export KUBECONFIG="${ATLAS_KUBECONFIG:-$HOME/.kube/atlas-${SERVER_IP}.yaml}"
set -a; [ -f .env ] && . ./.env; set +a
export NO_PROXY="${NO_PROXY:+${NO_PROXY},}${SERVER_IP}" no_proxy="${no_proxy:+${no_proxy},}${SERVER_IP}"
[ -n "${SERPAPI_KEY:-}" ] || { echo "✗ .env 没有 SERPAPI_KEY"; exit 1; }

echo "── 登记 serpapi（REST 转换，直连 serpapi.com）"
# ★ key 经标准输入进 Pod：不出现在命令行参数里
SERPAPI_KEY="$SERPAPI_KEY" python3 -c '
import os, sys
sys.stdout.write(open("deploy/server/mcp-serpapi.yaml", encoding="utf-8").read()
                 .replace("__SERPAPI_KEY__", os.environ["SERPAPI_KEY"]))' |
  kubectl -n atlas-system exec -i deploy/atlas-mcpgateway -- \
    python -m atlas_mcpgateway service add --file - --owner-account admin

echo "✓ 完成。到 MCP 管理端给智能体的 Key 分配 serpapi：http://atlas.${SERVER_IP}.nip.io/atlas/mcp"
