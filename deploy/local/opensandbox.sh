#!/usr/bin/env bash
# 本机 K8s（Docker Desktop）上的 OpenSandbox（doc/sandbox-opensandbox-design.html §17）：
#   S3 CSI 驱动 + 整桶静态 PV / PVC → CRD + 控制器 → 两个 lifecycle server：
#   opensandbox-sbx（原生沙箱，命名空间 atlas-sbx）、opensandbox-acp（ACP Pod，命名空间 atlas-acp）。
#
# 前置：
#   · make up（宿主的 MinIO）；仓库根 .env 里有 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET，
#     以及 ACP 的模型 key ATLAS_ACP_MODEL_API_KEY（没有就退回旧名 ATLAS_CLUSTER_MODEL_API_KEY）
#   · 镜像 atlas-csi-s3:0.43.7（docker/csi-s3/Dockerfile）、atlas-sandbox:dev（docker/sandbox/Dockerfile）
#   · helm 3；OpenSandbox 源码检出在 release-1.1.1（上游 chart 不单独发布，只能从源码装）：
#       git clone --depth 1 --branch release-1.1.1 https://github.com/opensandbox-group/OpenSandbox.git
#     用 OPENSANDBOX_SRC 指向它
#
# ★ 凭据都从 .env / 集群里来，建成 Secret，不进 YAML。
# ★ 幂等：重复执行只更新有变化的部分。API key 已存在就沿用（atlas-server 手里那份不能变）。
set -euo pipefail
cd "$(dirname "$0")/../.."

: "${OPENSANDBOX_SRC:?OPENSANDBOX_SRC 要指向 OpenSandbox 源码（release-1.1.1）}"
HELM="${HELM:-helm}"
CHARTS="$OPENSANDBOX_SRC/manifests/charts"
[ -d "$CHARTS/server" ] || { echo "$CHARTS 下没有 chart"; exit 1; }

[ -f .env ] || { echo "缺 .env"; exit 1; }
# shellcheck disable=SC1091
set -a; . ./.env; set +a

# ★ 宿主地址用 LAN IP（同 apply.sh）：CSI 节点插件从 Pod 网络连宿主的 MinIO。
#   服务器部署时 ATLAS_OSS_ADDR 给集群内 MinIO 的服务名（deploy/server/deploy.sh）
HOST_IP="${ATLAS_HOST_IP:-$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1)}"
[ -n "$HOST_IP" ] || { echo "取不到宿主 LAN IP，手动给 ATLAS_HOST_IP="; exit 1; }

kubectl apply -f deploy/local/00-namespaces.yaml

# ── 1. S3 CSI 驱动 + 存储 ────────────────────────────────────────────
kubectl -n kube-system create secret generic atlas-oss-csi \
  --from-literal=accessKeyID="${OSS_ACCESS_KEY_ID}" \
  --from-literal=secretAccessKey="${OSS_ACCESS_KEY_SECRET}" \
  --from-literal=endpoint="http://${ATLAS_OSS_ADDR:-${HOST_IP}:9000}" \
  --from-literal=region="${OSS_REGION:-us-east-1}" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f deploy/local/05-csi-s3.yaml
kubectl -n kube-system rollout status ds/csi-s3 --timeout=180s
kubectl apply -f deploy/local/06-sbx-storage.yaml
kubectl apply -f deploy/local/07-acp-storage.yaml

# ── 2. CRD + 控制器（全集群一个）────────────────────────────────────
"$HELM" upgrade --install opensandbox-base "$CHARTS/base"
"$HELM" upgrade --install opensandbox-controller "$CHARTS/controller" -n opensandbox-system
kubectl -n opensandbox-system rollout status deploy/opensandbox-controller-manager --timeout=180s

# ── 3. 两个 lifecycle server ────────────────────────────────────────
# API key 已存在就沿用（atlas-server 手里那份不能变）
api_key() {
  local key
  key="$(kubectl -n opensandbox-system get secret "opensandbox-$1-api-key" \
    -o jsonpath='{.data.api-key}' 2>/dev/null | base64 --decode || true)"
  [ -n "$key" ] || key="$(openssl rand -hex 24)"
  echo "$key"
}

install_server() {  # $1 = sbx | acp
  local kind="$1" key="$2" sha
  kubectl -n opensandbox-system create secret generic "opensandbox-${kind}-api-key" \
    --from-literal=api-key="${key}" --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n opensandbox-system create configmap "opensandbox-${kind}-template" \
    --from-file="${kind}.yaml=deploy/local/opensandbox/${kind}-template.yaml" \
    --dry-run=client -o yaml | kubectl apply -f -
  # ★ server 只在启动时读模板：模板的哈希进 Pod 注解，模板变了才滚动重启
  sha="$(shasum -a 256 "deploy/local/opensandbox/${kind}-template.yaml" | cut -c1-16)"
  "$HELM" upgrade --install "opensandbox-${kind}" "$CHARTS/server" -n opensandbox-system \
    -f "deploy/local/opensandbox/${kind}-values.yaml" \
    --set-string "server.podAnnotations.atlas/template-sha=${sha}"
  kubectl -n opensandbox-system rollout status "deploy/opensandbox-${kind}" --timeout=180s
}

SBX_KEY="$(api_key sbx)"
ACP_KEY="$(api_key acp)"
# atlas-server 用同样的 key（30-server.yaml 的 OPENSANDBOX_SBX_API_KEY / OPENSANDBOX_ACP_API_KEY）
kubectl -n atlas-system create secret generic atlas-opensandbox \
  --from-literal=sbx_api_key="${SBX_KEY}" --from-literal=acp_api_key="${ACP_KEY}" \
  --dry-run=client -o yaml | kubectl apply -f -
# ACP Pod 的模型 key：ACP 模板把它以文件挂进 Pod（/run/secrets/atlas/model_api_key）
MODEL_KEY="${ATLAS_ACP_MODEL_API_KEY:-${ATLAS_CLUSTER_MODEL_API_KEY:-}}"
[ -n "$MODEL_KEY" ] || echo "⚠ 没有 ACP 模型 key：ACP 会话的 CLI 将无法调用模型"
kubectl -n atlas-acp create secret generic atlas-acp-model \
  --from-literal=model_api_key="${MODEL_KEY}" --dry-run=client -o yaml | kubectl apply -f -

install_server sbx "$SBX_KEY"
install_server acp "$ACP_KEY"
echo "就绪：集群内 http://opensandbox-sbx.opensandbox-system、http://opensandbox-acp.opensandbox-system"
