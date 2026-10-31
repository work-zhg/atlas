#!/usr/bin/env bash
# 模型调用切到 LiteLLM + 模型管理平台（agent-platform/doc/design/model-design.html）的一次性步骤。
# 可重复执行。
#
#   deploy/server/model-cutover.sh
#
# 前置（deploy.sh 与 register.sh 已经做完）：
#   · atlas-litellm、atlas-modelgateway 在跑（模型管理平台的库已迁移到 m0002）
#   · register.sh 已把模型管理平台登记到用户中心（应用 model-gateway、数据编码 Model）
#   · .env 里有供应商 Key：DEEPSEEK_API_KEY（没有就用 LITELLM_KEY —— 直连时用的就是 DeepSeek 那把）
#
# 环境变量（都有默认值）：
#   MODELS          要登记的模型（DeepSeek），空格分隔，默认 "deepseek-v4-pro deepseek-flash"
#   SERVICE_MODEL   会话标题、记忆补记、知识库入库增强与查询理解用的模型，默认 deepseek-flash
#   AGENT_MODELS    给存量智能体的客户端 Key 分配的模型（逗号分隔，只分配还没分配过的），
#                   默认 = MODELS；设成空串就不分配（之后在模型管理端逐个分配）
#
# 做六件事：
#   1. 在模型管理平台登记模型（公有：所有人都能选进自己的用户 Key）
#   2. 平台服务的客户端 Key：Service:session-title、Service:memory-backfill、Service:knowledge-chat，
#      分配 SERVICE_MODEL；Service:knowledge-embedding、Service:knowledge-rerank 只建不分配
#      （DeepSeek 没有嵌入、重排模型；登记了再在模型管理端分配，建库时才有远程模型可选）
#   3. 建 ConfigMap atlas-model-platform，删掉自研网关时期的 atlas-model-gateway，
#      滚动重启 atlas-server 与知识库：都从此经平台
#   4. Atlas 里配平台服务模型（service-model set）
#   5. 存量智能体补建客户端 Key，按 AGENT_MODELS 分配
#   6. ACP Pod 里不放供应商 Key（Secret atlas-acp-model 清空）：Pod 拿 LiteLLM 地址和智能体的 Key
#
# ★ 回退：kubectl -n atlas-system delete configmap atlas-model-platform，再滚动重启 atlas-server；
#   ACP 的供应商 Key 要重跑 deploy/local/opensandbox.sh 恢复。
# ★ 自研网关时期 Higress 上的模型路由（deepseek 供应商与 AI 路由）不再使用，可在 Higress 控制台删掉。
set -euo pipefail
cd "$(dirname "$0")/../.."

SERVER_IP="${ATLAS_SERVER_IP:-192.168.0.112}"
export KUBECONFIG="${ATLAS_KUBECONFIG:-$HOME/.kube/atlas-${SERVER_IP}.yaml}"

[ -f .env ] || { echo "缺 .env"; exit 1; }
# shellcheck disable=SC1091
set -a; . ./.env; set +a
DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-${LITELLM_KEY:-}}"
[ -n "$DEEPSEEK_API_KEY" ] || { echo "缺供应商 Key（DEEPSEEK_API_KEY / LITELLM_KEY）"; exit 1; }
read -r -a MODEL_LIST <<< "${MODELS:-deepseek-v4-pro deepseek-flash}"
SERVICE_MODEL="${SERVICE_MODEL:-deepseek-flash}"
AGENT_MODELS="${AGENT_MODELS-$(IFS=,; echo "${MODEL_LIST[*]}")}"

platform() { kubectl -n atlas-system exec -i deploy/atlas-modelgateway -c modelgateway -- "$@"; }
atlas() { kubectl -n atlas-system exec deploy/atlas-server -- "$@"; }

echo "── 1. 在模型管理平台登记模型"
for m in "${MODEL_LIST[@]}"; do
  # 供应商 Key 经标准输入进 Pod 的环境变量，不出现在命令行和进程列表里
  printf '%s' "$DEEPSEEK_API_KEY" | platform sh -c \
    "DEEPSEEK_API_KEY=\"\$(cat)\" python -m atlas_modelgateway model add '$m' --provider deepseek \
     --visibility public --api-key-env DEEPSEEK_API_KEY --actor-account admin"
done
platform python -m atlas_modelgateway model list

echo "── 2. 平台服务的客户端 Key"
platform python -m atlas_modelgateway client-key ensure --type Service --code session-title \
  --name 会话标题 --models "$SERVICE_MODEL" --actor-account admin
platform python -m atlas_modelgateway client-key ensure --type Service --code memory-backfill \
  --name 记忆补记 --models "$SERVICE_MODEL" --actor-account admin
platform python -m atlas_modelgateway client-key ensure --type Service --code knowledge-chat \
  --name 知识库·入库增强与查询理解 --models "$SERVICE_MODEL" --actor-account admin
platform python -m atlas_modelgateway client-key ensure --type Service --code knowledge-embedding \
  --name 知识库·远程嵌入 --actor-account admin
platform python -m atlas_modelgateway client-key ensure --type Service --code knowledge-rerank \
  --name 知识库·远程重排 --actor-account admin

echo "── 3. Atlas、知识库切到模型管理平台"
kubectl -n atlas-system create configmap atlas-model-platform \
  --from-literal=platform_url="http://atlas-modelgateway.atlas-system.svc.cluster.local:8090" \
  --from-literal=litellm_url="http://atlas-litellm.atlas-system.svc.cluster.local:4000" \
  --from-literal=kb_enrich_model="$SERVICE_MODEL" \
  --dry-run=client -o yaml | kubectl apply -f -
# 自研网关时期的切换开关，已经没人认它
kubectl -n atlas-system delete configmap atlas-model-gateway --ignore-not-found
for d in atlas-server atlas-knowledge atlas-knowledge-worker; do
  kubectl -n atlas-system rollout restart "deploy/$d"
done
for d in atlas-server atlas-knowledge atlas-knowledge-worker; do
  kubectl -n atlas-system rollout status "deploy/$d" --timeout=300s
done

echo "── 4. Atlas 的平台服务模型"
atlas python -m atlas_agent service-model set title \
  --client-type Service --client-code session-title --model "$SERVICE_MODEL"
atlas python -m atlas_agent service-model set memory_backfill \
  --client-type Service --client-code memory-backfill --model "$SERVICE_MODEL"

echo "── 5. 存量智能体的客户端 Key"
AGENTS="$(atlas python -m atlas_agent register-agent-keys)"
printf '%s\n' "$AGENTS"
if [ -n "$AGENT_MODELS" ]; then
  while IFS=$'\t' read -r agent_id _status _name; do
    [ -n "$agent_id" ] || continue
    platform python -m atlas_modelgateway client-key ensure --type Agent --code "$agent_id" \
      --models "$AGENT_MODELS" --if-pending --actor-account admin </dev/null  # exec -i 会吃掉循环的输入
  done <<< "$AGENTS"
fi

echo "── 6. 撤掉 ACP Pod 里的供应商 Key"
kubectl -n atlas-acp create secret generic atlas-acp-model \
  --from-literal=model_api_key= --dry-run=client -o yaml | kubectl apply -f -

echo "✓ 完成。新建的智能体要在模型管理端给它的客户端 Key 分配模型之后才能运行。"
echo "  LiteLLM 管理界面（日志、花费）：kubectl -n atlas-system port-forward svc/atlas-litellm 4000:4000，"
echo "  打开 http://127.0.0.1:4000/ui（账号 admin，密码 = Secret atlas-litellm-creds 的 master_key）"
