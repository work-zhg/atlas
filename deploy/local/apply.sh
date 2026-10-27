#!/usr/bin/env bash
# 把用户中心、server、config、knowledge、memory、MCP 网关与 TeamFlow 后端部署到本机 K8s（Docker Desktop）。
#
# 前置：make up（宿主的 PG / Redis / MinIO）、镜像已构建：
#   docker build -f docker/bridge/Dockerfile -t atlas-acp-bridge:0.4.0 .   # ACP 会话的 CLI 镜像
#   uv export --frozen --no-dev --no-emit-workspace --all-packages --no-hashes -o docker/app/constraints.txt
#   docker build -f docker/app/Dockerfile    -t atlas-app:dev .
# 会话 Pod（原生沙箱、ACP）由 OpenSandbox 创建：先跑 deploy/local/opensandbox.sh。
#
# ★ 凭据从仓库根的 .env 读，建成 K8s Secret —— 不进任何 YAML，
#   因为 YAML 会进 git。
set -euo pipefail
cd "$(dirname "$0")/../.."

[ -f .env ] || { echo "缺 .env"; exit 1; }
# shellcheck disable=SC1091
set -a; . ./.env; set +a

kubectl apply -f deploy/local/00-namespaces.yaml

# ★ create --dry-run=client | apply：幂等。直接 create 第二次会 AlreadyExists，
#   而 apply -f 一个 Secret 字面量又会把凭据写进临时文件。
kubectl -n atlas-system create secret generic atlas-server-creds \
  --from-literal=oss_access_key_id="${OSS_ACCESS_KEY_ID}" \
  --from-literal=oss_access_key_secret="${OSS_ACCESS_KEY_SECRET}" \
  --from-literal=litellm_base_url="${LITELLM_BASE_URL}" \
  --from-literal=litellm_key="${LITELLM_KEY}" \
  --from-literal=acp_model_base_url="${ATLAS_ACP_MODEL_BASE_URL:-${ATLAS_CLUSTER_MODEL_BASE_URL:-}}" \
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

# MCP 网关的网关凭据（doc/mcp-design.html §8.3）：Higress 只认它，所以只有 MCP 网关能调 Higress。
# .env 里有就用；没有就沿用集群里已有的；都没有才新生成。★ 换了值要同时改 Higress 的配置
GW_CREDENTIAL="${ATLAS_MCPGW_GATEWAY_CREDENTIAL:-$(kubectl -n atlas-system get secret atlas-mcpgateway-creds \
  -o jsonpath='{.data.gateway_credential}' 2>/dev/null | base64 --decode || true)}"
[ -n "$GW_CREDENTIAL" ] || GW_CREDENTIAL="$(openssl rand -hex 24)"
kubectl -n atlas-system create secret generic atlas-mcpgateway-creds \
  --from-literal=gateway_credential="${GW_CREDENTIAL}" \
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

# TeamFlow 的 git 凭据档案（ACP Pod 里 git push 产物，经 Credential Vault）：.env 没有 Gitee 令牌就跳过
if [ -n "${ATLAS_GIT_TEAMFLOW_TOKEN:-${ATLAS_TF_GITEE_TOKEN:-}}" ]; then
  deploy/local/git-profile.sh
fi

# ★ 宿主地址用 LAN IP，不用 host.docker.internal —— 后者会被 Docker Desktop
#   转发到宿主 loopback，命中绑在 127.0.0.1 的本机服务而不是 compose 那个。
HOST_IP="${ATLAS_HOST_IP:-$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1)}"
[ -n "$HOST_IP" ] || { echo "取不到宿主 LAN IP，手动给 ATLAS_HOST_IP="; exit 1; }
echo "宿主地址 = $HOST_IP"

# 依赖服务的地址。本机默认是宿主上 compose 的端口；服务器部署（deploy/server/deploy.sh）换成集群内的服务名。
# ★ 清单里统一写 __HOST_IP__:<compose 端口>，按端口换成各自的地址
PG_ADDR="${ATLAS_PG_ADDR:-${HOST_IP}:5433}"
REDIS_ADDR="${ATLAS_REDIS_ADDR:-${HOST_IP}:6380}"
OSS_ADDR="${ATLAS_OSS_ADDR:-${HOST_IP}:9000}"
QDRANT_ADDR="${ATLAS_QDRANT_ADDR:-${HOST_IP}:6333}"
OTEL_ADDR="${ATLAS_OTEL_ADDR:-${HOST_IP}:4318}"
HIGRESS_ADDR="${ATLAS_HIGRESS_ADDR:-${HOST_IP}:18080}"
PUBLIC_ORIGIN="${ATLAS_PUBLIC_ORIGIN:-http://localhost:8080}"
render() {
  sed -e "s#__HOST_IP__:5433#${PG_ADDR}#g" -e "s#__HOST_IP__:6380#${REDIS_ADDR}#g" \
      -e "s#__HOST_IP__:9000#${OSS_ADDR}#g" -e "s#__HOST_IP__:6333#${QDRANT_ADDR}#g" \
      -e "s#__HOST_IP__:4318#${OTEL_ADDR}#g" -e "s#__HOST_IP__:18080#${HIGRESS_ADDR}#g" \
      -e "s#__PUBLIC_ORIGIN__#${PUBLIC_ORIGIN}#g" -e "s#__HOST_IP__#${HOST_IP}#g" "$1"
}

# 一次性（内网身份传递 §4）：后端 Service 从 LoadBalancer 改为 ClusterIP，只经网关对外。
# 直接 apply 改不了类型（已分配的 nodePort 会被拒），先删旧的再建 —— 只影响这次 apply 的几秒。
for svc in atlas-usercenter atlas-config atlas-server atlas-teamflow; do
  if [ "$(kubectl -n atlas-system get svc "$svc" -o jsonpath='{.spec.type}' 2>/dev/null)" = "LoadBalancer" ]; then
    kubectl -n atlas-system delete svc "$svc"
  fi
done

# 用户中心先起：server / knowledge / TeamFlow 都依赖它
render deploy/local/15-usercenter.yaml | kubectl apply -f -
kubectl -n atlas-system rollout status deploy/atlas-usercenter --timeout=180s
render deploy/local/25-config.yaml  | kubectl apply -f -
render deploy/local/36-memory.yaml  | kubectl apply -f -
render deploy/local/37-mcpgateway.yaml | kubectl apply -f -
render deploy/local/30-server.yaml  | kubectl apply -f -
render deploy/local/35-knowledge.yaml | kubectl apply -f -
render deploy/local/45-teamflow.yaml | kubectl apply -f -

kubectl -n atlas-system rollout status deploy/atlas-config --timeout=180s
kubectl -n atlas-system rollout status deploy/atlas-memory --timeout=180s
kubectl -n atlas-system rollout status deploy/atlas-mcpgateway --timeout=180s
kubectl -n atlas-system rollout status deploy/atlas-server --timeout=180s
kubectl -n atlas-system rollout status deploy/atlas-knowledge --timeout=300s
kubectl -n atlas-system rollout status deploy/atlas-knowledge-worker --timeout=300s
kubectl -n atlas-system rollout status deploy/atlas-teamflow --timeout=180s
echo "后端只经网关对外（deploy/local/frontends.sh → http://localhost:8080）；"
echo "运维直连用 kubectl -n atlas-system port-forward svc/<服务> <端口>（评测见 forward.sh）"
