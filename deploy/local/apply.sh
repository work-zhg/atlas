#!/usr/bin/env bash
# 把 server 与 cluster 部署到本机 K8s（Docker Desktop）。
#
# 前置：make up（宿主的 PG / Redis / MinIO）、两个镜像已构建：
#   docker build -f docker/bridge/Dockerfile -t atlas-acp-bridge:0.1.0 .
#   docker build -f docker/app/Dockerfile    -t atlas-app:dev .
#
# ★ 凭据从仓库根的 .env 读，建成 K8s Secret —— 不进任何 YAML，
#   因为 YAML 会进 git。
set -euo pipefail
cd "$(dirname "$0")/../.."

[ -f .env ] || { echo "缺 .env"; exit 1; }
# shellcheck disable=SC1091
set -a; . ./.env; set +a

kubectl apply -f deploy/local/00-namespaces.yaml
kubectl apply -f deploy/local/10-cluster-rbac.yaml

# ★ create --dry-run=client | apply：幂等。直接 create 第二次会 AlreadyExists，
#   而 apply -f 一个 Secret 字面量又会把凭据写进临时文件。
kubectl -n atlas-system create secret generic atlas-cluster-creds \
  --from-literal=oss_access_key_id="${OSS_ACCESS_KEY_ID}" \
  --from-literal=oss_secret_access_key="${OSS_ACCESS_KEY_SECRET}" \
  --from-literal=model_base_url="${ATLAS_CLUSTER_MODEL_BASE_URL}" \
  --from-literal=model_api_key="${ATLAS_CLUSTER_MODEL_API_KEY}" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n atlas-system create secret generic atlas-server-creds \
  --from-literal=oss_access_key_id="${OSS_ACCESS_KEY_ID}" \
  --from-literal=oss_access_key_secret="${OSS_ACCESS_KEY_SECRET}" \
  --from-literal=litellm_base_url="${LITELLM_BASE_URL}" \
  --from-literal=litellm_key="${LITELLM_KEY}" \
  --dry-run=client -o yaml | kubectl apply -f -

# ★ 宿主地址用 LAN IP，不用 host.docker.internal —— 后者会被 Docker Desktop
#   转发到宿主 loopback，命中绑在 127.0.0.1 的本机服务而不是 compose 那个。
HOST_IP="${ATLAS_HOST_IP:-$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1)}"
[ -n "$HOST_IP" ] || { echo "取不到宿主 LAN IP，手动给 ATLAS_HOST_IP="; exit 1; }
echo "宿主地址 = $HOST_IP"

sed "s/__HOST_IP__/${HOST_IP}/g" deploy/local/20-cluster.yaml | kubectl apply -f -
sed "s/__HOST_IP__/${HOST_IP}/g" deploy/local/30-server.yaml  | kubectl apply -f -

kubectl -n atlas-system rollout status deploy/atlas-cluster --timeout=180s
kubectl -n atlas-system rollout status deploy/atlas-server --timeout=180s
echo "server → http://127.0.0.1:8000"
