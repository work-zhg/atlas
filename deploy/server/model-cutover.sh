#!/usr/bin/env bash
# 模型调用切到自研模型网关（doc/model-design.html §6.3「存量迁移」）的一次性步骤。可重复执行。
#
#   deploy/server/model-cutover.sh [--no-all-users]
#
# 前置（deploy.sh 与 register.sh 已经做完）：
#   · atlas-modelgateway 在跑；Secret atlas-modelgateway-creds 里有网关凭据
#   · register.sh 已把模型网关登记到用户中心（应用 model-gateway、数据编码 Model）
#   · .env 里有供应商 Key：DEEPSEEK_API_KEY（没有就用 LITELLM_KEY —— 现在直连用的就是 DeepSeek 那把）
#
# 做五件事：
#   1. Higress：供应商（Key 只在这里）+ AI 路由（按模型名匹配，只认模型网关的凭据）。
#      模型名默认 deepseek-chat，可用 MODELS="deepseek-chat deepseek-v4-pro" 覆盖
#   2. 在 Atlas 里导出模型目录、数字员工用到的模型、平台用途（export-models）
#   3. 导入模型网关（import，注册人 / 操作人 = 用户中心的 admin）：登记模型、上线原来可用的；
#      Higress 上没有路由的模型接入检查不过，会被跳过（报出来）。默认给根部门（含下级）授「使用」，
#      保持切换前「谁都能用」（§6.3）；--no-all-users 只给所有者（admin），其他人在模型管理端逐个授权
#   4. 建 ConfigMap atlas-model-gateway，滚动重启 atlas-server 与知识库：它们从此只经模型网关
#   5. 撤掉 ACP Pod 里的供应商 Key（Secret atlas-acp-model 清空）：Pod 改拿网关地址和主体 Key
#
# ★ 做完之后在模型管理端给每个模型跑一次能力探测、确认（§4.4），并填上价目（不填成本显示为未知）。
# ★ 回退：kubectl -n atlas-system delete configmap atlas-model-gateway，再滚动重启两者；
#   ACP 的供应商 Key 要重跑 deploy/local/opensandbox.sh 恢复。
set -euo pipefail
cd "$(dirname "$0")/../.."

SERVER_IP="${ATLAS_SERVER_IP:-192.168.0.112}"
export KUBECONFIG="${ATLAS_KUBECONFIG:-$HOME/.kube/atlas-${SERVER_IP}.yaml}"
ALL_USERS="--all-users"
[ "${1:-}" = "--no-all-users" ] && ALL_USERS=""

[ -f .env ] || { echo "缺 .env"; exit 1; }
# shellcheck disable=SC1091
set -a; . ./.env; set +a
export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-${LITELLM_KEY:-}}"
[ -n "$DEEPSEEK_API_KEY" ] || { echo "缺供应商 Key（DEEPSEEK_API_KEY / LITELLM_KEY）"; exit 1; }
read -r -a MODEL_LIST <<< "${MODELS:-deepseek-chat}"

echo "── 1. Higress：供应商与 AI 路由（只认模型网关）"
kubectl -n atlas-infra port-forward svc/higress 28001:8001 >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null || true' EXIT
for _ in $(seq 60); do curl -s -o /dev/null --noproxy '*' http://127.0.0.1:28001 && break; sleep 2; done
MODEL_ARGS=()
for m in "${MODEL_LIST[@]}"; do MODEL_ARGS+=(--model "$m"); done
MODEL_GATEWAY_CREDENTIAL="$(kubectl -n atlas-system get secret atlas-modelgateway-creds \
  -o jsonpath='{.data.gateway_credential}' | base64 --decode)" \
HIGRESS_CONSOLE=http://127.0.0.1:28001 \
  python3 deploy/local/higress/models.py --provider deepseek --type deepseek \
    --token-env DEEPSEEK_API_KEY "${MODEL_ARGS[@]}"
kill $PF 2>/dev/null || true

echo "── 2. 从 Atlas 导出"
EXPORTED="$(mktemp)"
kubectl -n atlas-system exec deploy/atlas-server -- python -m atlas_agent export-models > "$EXPORTED"
python3 - "$EXPORTED" <<'EOF'
import json, sys
data = json.load(open(sys.argv[1]))
print(f"  模型 {len(data['models'])} 个，以 Agent 为权限的数字员工 {len(data['digital_employees'])} 个")
if data.get("unknown_models"):
    print("  ⚠ Agent 引用了、但模型目录里没有：" + "、".join(data["unknown_models"]))
EOF

echo "── 3. 导入模型网关"
# shellcheck disable=SC2086
kubectl -n atlas-system exec -i deploy/atlas-modelgateway -- sh -c \
  "cat > /tmp/atlas-models.json && python -m atlas_modelgateway import --file /tmp/atlas-models.json \
   --owner-account admin ${ALL_USERS}" < "$EXPORTED" || echo "⚠ 有模型没导入（见上面的 ✗）"
rm -f "$EXPORTED"

echo "── 4. Atlas、知识库切到模型网关"
kubectl -n atlas-system create configmap atlas-model-gateway \
  --from-literal=url="http://atlas-modelgateway.atlas-system.svc.cluster.local:8090" \
  --dry-run=client -o yaml | kubectl apply -f -
for d in atlas-server atlas-knowledge atlas-knowledge-worker; do
  kubectl -n atlas-system rollout restart "deploy/$d"
done
for d in atlas-server atlas-knowledge atlas-knowledge-worker; do
  kubectl -n atlas-system rollout status "deploy/$d" --timeout=300s
done

echo "── 5. 撤掉 ACP Pod 里的供应商 Key"
kubectl -n atlas-acp create secret generic atlas-acp-model \
  --from-literal=model_api_key= --dry-run=client -o yaml | kubectl apply -f -

echo "✓ 完成。到模型管理端给每个模型跑能力探测并确认、填价目：http://atlas.${SERVER_IP}.nip.io/atlas/models"
