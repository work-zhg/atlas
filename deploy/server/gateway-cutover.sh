#!/usr/bin/env bash
# 从 LiteLLM 时期升级到自研模型网关（agent-platform/doc/design/model-gateway-design.html）之后的一次性收尾。
# 可重复执行。日本服务器 2026-11-20 已做过。
#
#   deploy/server/gateway-cutover.sh
#
# 前置：
#   · deploy.sh 已部署新镜像：管理平台迁移到 m0004（.env 有 TENANT_BACKFILL_UUID），atlas-llmgateway 在跑；
#     Atlas、知识库、Pod 里的 CLI 都连网关（清单里写死了网关地址），LiteLLM 已被 apply.sh 删掉
#   · 模型管理端（{域名}/atlas/models）已登记供应商账号，每个启用的模型都有直连线路 —— 没有线路的模型调不通
#
# 环境变量：
#   TENANT          在哪个租户里做，默认 .env 的 TENANT_BACKFILL_UUID（原平台租户）
#   SERVICE_MODEL   冒烟用的模型，默认 deepseek-flash（用 Service:session-title 的 Key 调）
#
# 做四件事：
#   1. 已有 Key 的原文：客户端 Key 改成加密存，用户 Key 删掉原文（只留摘要，原 Key 照样能用）
#   2. 检查没有线路的模型：有就停在这里，先去模型管理端加线路
#   3. 重算用户 Key 的有效模型
#   4. 冒烟：用 Service:session-title 的 Key 经网关的通用接口调一次 SERVICE_MODEL
set -euo pipefail
cd "$(dirname "$0")/../.."

SERVER_IP="${ATLAS_SERVER_IP:-192.168.0.112}"
# 在服务器上直接运行时用本机的 admin.conf
if [ -z "${ATLAS_KUBECONFIG:-}" ] && [ -r /etc/kubernetes/admin.conf ]; then ATLAS_KUBECONFIG=/etc/kubernetes/admin.conf; fi
export KUBECONFIG="${ATLAS_KUBECONFIG:-$HOME/.kube/atlas-${SERVER_IP}.yaml}"

[ -f .env ] || { echo "缺 .env"; exit 1; }
# shellcheck disable=SC1091
set -a; . ./.env; set +a
TENANT="${TENANT:-${TENANT_BACKFILL_UUID:-}}"
[ -n "$TENANT" ] || { echo "✗ 没有租户：给 TENANT= 或在 .env 写 TENANT_BACKFILL_UUID"; exit 1; }
SERVICE_MODEL="${SERVICE_MODEL:-deepseek-flash}"

platform() { kubectl -n atlas-system exec -i deploy/atlas-modelgateway -c modelgateway -- "$@"; }
gateway() { kubectl -n atlas-system exec deploy/atlas-llmgateway -c gateway -- "$@"; }

echo "── 1. 已有 Key 的原文"
platform python -m atlas_modelgateway encrypt-secrets

echo "── 2. 模型与线路"
MODELS="$(platform python -m atlas_modelgateway model list --tenant "$TENANT")"
printf '%s\n' "$MODELS"
if printf '%s\n' "$MODELS" | grep -P '\t启用\t（没有线路）$' >/dev/null; then
  echo "✗ 上面有启用的模型没有线路：在模型管理端给它们加直连账号的线路后再运行"
  exit 1
fi

echo "── 3. 重算用户 Key 的有效模型"
platform python -m atlas_modelgateway refresh-keys

echo "── 4. 冒烟（经网关调一次 ${SERVICE_MODEL}）"
gateway python -m atlas_modelgateway smoke --tenant "$TENANT" \
  --client Service:session-title --model "$SERVICE_MODEL"

echo "✓ 完成。Atlas（含 Pod 里的 CLI）、知识库的模型调用都经 atlas-llmgateway 直连供应商。"
