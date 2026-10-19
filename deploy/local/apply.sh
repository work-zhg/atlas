#!/usr/bin/env bash
# 把用户中心、server、cluster、config、knowledge 与 TeamFlow 后端部署到本机 K8s（Docker Desktop）。
#
# 前置：make up（宿主的 PG / Redis / MinIO）、两个镜像已构建：
#   docker build -f docker/bridge/Dockerfile -t atlas-acp-bridge:0.4.0 .
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

# 知识库 MCP 令牌的签名密钥（atlas-server 签、知识库验）：给会话 Pod 的能力凭据（§5.3）。
# .env 里有就用；没有就沿用集群里已有的（含改名前 atlas-config-creds 里的那份，已签发的令牌
# 继续有效）；都没有才新生成。★ 必须在重建 atlas-config-creds 之前读：apply 会删掉旧键。
MCP_SECRET="${ATLAS_KNOWLEDGE_MCP_SECRET:-$(kubectl -n atlas-system get secret atlas-knowledge-mcp \
  -o jsonpath='{.data.secret}' 2>/dev/null | base64 --decode || true)}"
[ -n "$MCP_SECRET" ] || MCP_SECRET="$(kubectl -n atlas-system get secret atlas-config-creds \
  -o jsonpath='{.data.internal_token}' 2>/dev/null | base64 --decode || true)"
[ -n "$MCP_SECRET" ] || MCP_SECRET="$(openssl rand -hex 24)"
kubectl -n atlas-system create secret generic atlas-knowledge-mcp \
  --from-literal=secret="${MCP_SECRET}" \
  --dry-run=client -o yaml | kubectl apply -f -

# ★ 内网免令牌（doc/identity-propagation-design.html）：服务间令牌、用户中心 App Key / Secret、
#   Atlas 服务账号密钥都已下线，这里不再建。
kubectl -n atlas-system create secret generic atlas-config-creds \
  --from-literal=oss_access_key_id="${OSS_ACCESS_KEY_ID}" \
  --from-literal=oss_access_key_secret="${OSS_ACCESS_KEY_SECRET}" \
  --dry-run=client -o yaml | kubectl apply -f -

# TeamFlow 后端的凭据：只剩 Gitee 令牌（迁到 GitLab 后换成 GitLab 的项目管理令牌）
kubectl -n atlas-system create secret generic atlas-teamflow-creds \
  --from-literal=gitee_token="${ATLAS_TF_GITEE_TOKEN:-}" \
  --dry-run=client -o yaml | kubectl apply -f -

# TeamFlow 的 git 凭据档案（会话 Pod 里 git push 产物）：.env 没有 Gitee 令牌就跳过
if [ -n "${ATLAS_GIT_TEAMFLOW_TOKEN:-${ATLAS_TF_GITEE_TOKEN:-}}" ]; then
  deploy/local/git-profile.sh
fi

# ★ 宿主地址用 LAN IP，不用 host.docker.internal —— 后者会被 Docker Desktop
#   转发到宿主 loopback，命中绑在 127.0.0.1 的本机服务而不是 compose 那个。
HOST_IP="${ATLAS_HOST_IP:-$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1)}"
[ -n "$HOST_IP" ] || { echo "取不到宿主 LAN IP，手动给 ATLAS_HOST_IP="; exit 1; }
echo "宿主地址 = $HOST_IP"

# 一次性（内网身份传递 §4）：后端 Service 从 LoadBalancer 改为 ClusterIP，只经网关对外。
# 直接 apply 改不了类型（已分配的 nodePort 会被拒），先删旧的再建 —— 只影响这次 apply 的几秒。
for svc in atlas-usercenter atlas-config atlas-server atlas-teamflow; do
  if [ "$(kubectl -n atlas-system get svc "$svc" -o jsonpath='{.spec.type}' 2>/dev/null)" = "LoadBalancer" ]; then
    kubectl -n atlas-system delete svc "$svc"
  fi
done

# 用户中心先起：server / knowledge / TeamFlow 都依赖它
sed "s/__HOST_IP__/${HOST_IP}/g" deploy/local/15-usercenter.yaml | kubectl apply -f -
kubectl -n atlas-system rollout status deploy/atlas-usercenter --timeout=180s
sed "s/__HOST_IP__/${HOST_IP}/g" deploy/local/20-cluster.yaml | kubectl apply -f -
sed "s/__HOST_IP__/${HOST_IP}/g" deploy/local/25-config.yaml  | kubectl apply -f -
sed "s/__HOST_IP__/${HOST_IP}/g" deploy/local/30-server.yaml  | kubectl apply -f -
sed "s/__HOST_IP__/${HOST_IP}/g" deploy/local/35-knowledge.yaml | kubectl apply -f -
sed "s/__HOST_IP__/${HOST_IP}/g" deploy/local/45-teamflow.yaml | kubectl apply -f -

kubectl -n atlas-system rollout status deploy/atlas-cluster --timeout=180s
kubectl -n atlas-system rollout status deploy/atlas-config --timeout=180s
kubectl -n atlas-system rollout status deploy/atlas-server --timeout=180s
kubectl -n atlas-system rollout status deploy/atlas-knowledge --timeout=300s
kubectl -n atlas-system rollout status deploy/atlas-knowledge-worker --timeout=300s
kubectl -n atlas-system rollout status deploy/atlas-teamflow --timeout=180s
echo "后端只经网关对外（deploy/local/frontends.sh → http://localhost:8080）；"
echo "运维直连用 kubectl -n atlas-system port-forward svc/<服务> <端口>（评测见 forward.sh）"
