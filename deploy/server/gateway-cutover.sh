#!/usr/bin/env bash
# 模型调用从 LiteLLM 切到自研模型网关（agent-platform/doc/design/model-gateway-design.html 第 13 节）的
# 一次性步骤。可重复执行。
#
#   deploy/server/gateway-cutover.sh
#
# 前置：
#   · deploy.sh 已部署新镜像：atlas-modelgateway 的库已迁移到 m0004（.env 有 TENANT_BACKFILL_UUID，
#     apply.sh 建了 ConfigMap atlas-tenancy），atlas-llmgateway 在跑
#   · model-cutover.sh 早先跑过：平台服务的客户端 Key（Service:session-title …）已建好、分配了模型
#
# 环境变量：
#   TENANT          在哪个租户里做，默认 .env 的 TENANT_BACKFILL_UUID（原平台租户）
#   SERVICE_MODEL   冒烟用的模型，默认 deepseek-flash（用 Service:session-title 的 Key 调）
#
# 做六件事：
#   1. 已有 Key 的原文：客户端 Key 改成加密存，用户 Key 删掉原文（只留摘要，原 Key 照样能用）
#   2. 放行 LiteLLM 的 ClusterIP（只放这一个地址，不放整个 Service 网段），网关和管理平台滚动重启
#   3. 建过渡账号「LiteLLM（过渡）」：还没有线路的模型各加一条经 LiteLLM 的线路（上游模型名 = 模型名）
#   4. 重算用户 Key 的有效模型
#   5. 冒烟：用 Service:session-title 的 Key 经网关的通用接口调一次 SERVICE_MODEL —— 不通就停在这里，Atlas 不切
#   6. Atlas 改连网关的通用接口（ConfigMap atlas-model-platform 加 gateway_url = …:8091/universal），
#      滚动重启 atlas-server
#
# ★ 知识库这次不切：它的服务级客户端 Key 早就在 LiteLLM 里，照旧直连 LiteLLM。要切先按租户改造知识库
#   （网关的 /internal 要带 X-Tenant-Id，否则知识库取不到 Key），再把它的 ATLAS_KB_MODEL_LITELLM_URL 指向网关。
# ★ 之后在模型管理端给模型加直连供应商的账号和线路，确认没问题再停掉经 LiteLLM 的线路。
# ★ 回退：删掉 gateway_url 再滚动重启 atlas-server，Atlas 直连 LiteLLM：
#     kubectl -n atlas-system patch configmap atlas-model-platform --type=json \
#       -p '[{"op":"remove","path":"/data/gateway_url"}]'
#   只对切换前就有的 Key 有效 —— 管理平台不再同步给 LiteLLM，之后新建、重置的 Key 在 LiteLLM 里不存在。
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
# Atlas 连通用接口（Anthropic SDK / Pod 里 CLI 的 base_url）：模型的线路是哪种格式，网关都按线路转换
GATEWAY_URL="http://atlas-llmgateway.atlas-system.svc.cluster.local:8091/universal"
LITELLM_URL="http://atlas-litellm.atlas-system.svc.cluster.local:4000"

platform() { kubectl -n atlas-system exec -i deploy/atlas-modelgateway -c modelgateway -- "$@"; }
gateway() { kubectl -n atlas-system exec deploy/atlas-llmgateway -c gateway -- "$@"; }

echo "── 1. 已有 Key 的原文"
platform python -m atlas_modelgateway encrypt-secrets

echo "── 2. 放行 LiteLLM 的 ClusterIP"
LITELLM_IP="$(kubectl -n atlas-system get svc atlas-litellm -o jsonpath='{.spec.clusterIP}')"
[ -n "$LITELLM_IP" ] || { echo "✗ 找不到 Service atlas-litellm"; exit 1; }
kubectl -n atlas-system create configmap atlas-modelgw-config \
  --from-literal=upstream_allow_cidrs="[\"${LITELLM_IP}/32\"]" \
  --dry-run=client -o yaml | kubectl apply -f -
for d in atlas-modelgateway atlas-llmgateway; do
  kubectl -n atlas-system rollout restart "deploy/$d"
done
for d in atlas-modelgateway atlas-llmgateway; do
  kubectl -n atlas-system rollout status "deploy/$d" --timeout=300s
done

echo "── 3. 过渡账号：已有模型先经 LiteLLM"
# LiteLLM 的 master key 经标准输入进 Pod 的环境变量，不出现在命令行和进程列表里
kubectl -n atlas-system get secret atlas-litellm-creds -o jsonpath='{.data.master_key}' | base64 --decode \
  | platform sh -c "LITELLM_MASTER_KEY=\"\$(cat)\" python -m atlas_modelgateway bootstrap-litellm \
      --tenant '$TENANT' --url '$LITELLM_URL' --key-env LITELLM_MASTER_KEY --actor-account admin"
platform python -m atlas_modelgateway model list --tenant "$TENANT"

echo "── 4. 重算用户 Key 的有效模型"
platform python -m atlas_modelgateway refresh-keys

echo "── 5. 冒烟（经网关调一次 ${SERVICE_MODEL}）"
# 配置改动后网关最多 1 秒就看到（版本号）；这里多等一下，避开刚滚动完的实例还在预热
sleep 3
gateway python -m atlas_modelgateway smoke --tenant "$TENANT" \
  --client Service:session-title --model "$SERVICE_MODEL"

echo "── 6. Atlas 改连网关"
kubectl -n atlas-system patch configmap atlas-model-platform --type=merge \
  -p "{\"data\":{\"gateway_url\":\"${GATEWAY_URL}\"}}"
kubectl -n atlas-system rollout restart deploy/atlas-server
kubectl -n atlas-system rollout status deploy/atlas-server --timeout=300s

echo "✓ 完成。Atlas（含 Pod 里的 CLI）的模型调用经 atlas-llmgateway；已有模型的线路暂时都经 LiteLLM。"
echo "  下一步：模型管理端 → 账号，登记供应商账号；模型 → 线路，加直连线路后再停掉经 LiteLLM 的。"
