#!/usr/bin/env bash
# 把 Atlas 全套部署到服务器（单节点 K8s，默认 192.168.0.112）。全新部署：数据不迁移。
#
#   deploy/server/deploy.sh          全部
#   deploy/server/deploy.sh infra    只部署依赖服务（第 1 步；应用镜像还没导入时可以先做）
#
# 前置（都在本机）：
#   · ssh root@<服务器> 免密；本机有 kubectl、helm（不在 PATH 就用 HELM=… 指定）
#   · 镜像已导入服务器：deploy/server/images.sh
#   · 仓库根 .env（模型 key、Gitee 令牌、OSS 口令等 —— 与本机部署同一份）
#   · OpenSandbox 源码 release-1.1.1（OPENSANDBOX_SRC=…；不给就 clone 到 ~/.cache/atlas/opensandbox）
#
# 步骤：kubeconfig → 依赖服务（10-infra）→ OpenSandbox（opensandbox.sh）→ server 迁移 → 后端（apply.sh）
#   → 前端（frontends.sh）→ Ingress → Higress 的 MCP（setup.py）→ 用户中心首个超级管理员（只第一次）
#
# ★ 复用 deploy/local 的清单与脚本：依赖服务的地址经 ATLAS_*_ADDR 换成集群内的服务名（apply.sh 的 render）。
# ★ 幂等：重复执行只更新有变化的部分；随机生成的密钥已存在就沿用。
set -euo pipefail
cd "$(dirname "$0")/../.."

SERVER_IP="${ATLAS_SERVER_IP:-192.168.0.112}"
TARGET="root@${SERVER_IP}"
PUBLIC_HOST="atlas.${SERVER_IP}.nip.io"
# ★ 内网直连，不走开发机代理（本机 shell 里有 HTTPS_PROXY，kubectl 会被它带走）
export NO_PROXY="${NO_PROXY:+${NO_PROXY},}${SERVER_IP}" no_proxy="${no_proxy:+${no_proxy},}${SERVER_IP}"

KCFG="${ATLAS_KUBECONFIG:-$HOME/.kube/atlas-${SERVER_IP}.yaml}"
if [ ! -s "$KCFG" ]; then
  mkdir -p "$(dirname "$KCFG")"
  ssh "$TARGET" cat /etc/kubernetes/admin.conf > "$KCFG"
  chmod 600 "$KCFG"
fi
export KUBECONFIG="$KCFG"
kubectl get nodes >/dev/null

[ -f .env ] || { echo "缺 .env"; exit 1; }
# shellcheck disable=SC1091
set -a; . ./.env; set +a

INFRA=atlas-infra.svc.cluster.local
secret_value() {  # $1 命名空间 $2 Secret $3 键 —— 不存在输出空
  kubectl -n "$1" get secret "$2" -o jsonpath="{.data.$3}" 2>/dev/null | base64 --decode || true
}
keep_or_new() {  # 已有就沿用，否则生成；$4 = 生成命令
  local v; v="$(secret_value "$1" "$2" "$3")"
  [ -n "$v" ] || v="$(eval "$4")"
  echo "$v"
}

# ── 1. 依赖服务 ─────────────────────────────────────────────────────
kubectl apply -f deploy/local/00-namespaces.yaml
kubectl create namespace atlas-infra --dry-run=client -o yaml | kubectl apply -f -
kubectl -n atlas-infra create secret generic minio \
  --from-literal=user="${OSS_ACCESS_KEY_ID}" --from-literal=password="${OSS_ACCESS_KEY_SECRET}" \
  --dry-run=client -o yaml | kubectl apply -f -

CH_PW="$(keep_or_new atlas-infra langfuse clickhouse_password 'openssl rand -hex 16')"
LF_REDIS_PW="$(keep_or_new atlas-infra langfuse redis_password 'openssl rand -hex 16')"
LF_PK="$(keep_or_new atlas-infra langfuse public_key 'echo pk-lf-$(openssl rand -hex 12)')"
LF_SK="$(keep_or_new atlas-infra langfuse secret_key 'echo sk-lf-$(openssl rand -hex 24)')"
LF_PW="$(keep_or_new atlas-infra langfuse admin_password 'openssl rand -base64 12 | tr -d /+=')"
LF_SALT="$(keep_or_new atlas-infra langfuse salt 'openssl rand -hex 16')"
LF_ENC="$(keep_or_new atlas-infra langfuse encryption_key 'openssl rand -hex 32')"
LF_NEXTAUTH="$(keep_or_new atlas-infra langfuse nextauth_secret 'openssl rand -hex 24')"
kubectl -n atlas-infra create secret generic langfuse \
  --from-literal=clickhouse_password="$CH_PW" --from-literal=redis_password="$LF_REDIS_PW" \
  --from-literal=public_key="$LF_PK" --from-literal=secret_key="$LF_SK" \
  --from-literal=admin_password="$LF_PW" --from-literal=salt="$LF_SALT" \
  --from-literal=encryption_key="$LF_ENC" --from-literal=nextauth_secret="$LF_NEXTAUTH" \
  --from-literal=otlp_auth="Basic $(printf '%s:%s' "$LF_PK" "$LF_SK" | base64)" \
  --dry-run=client -o yaml | kubectl apply -f -
# web 与 worker 的环境里要用到的那部分（envFrom）
kubectl -n atlas-infra create secret generic langfuse-runtime \
  --from-literal=DATABASE_URL="postgresql://atlas:atlas@postgres:5432/langfuse" \
  --from-literal=SALT="$LF_SALT" --from-literal=ENCRYPTION_KEY="$LF_ENC" \
  --from-literal=CLICKHOUSE_PASSWORD="$CH_PW" --from-literal=REDIS_AUTH="$LF_REDIS_PW" \
  --from-literal=NEXTAUTH_URL="http://langfuse.${SERVER_IP}.nip.io" --from-literal=NEXTAUTH_SECRET="$LF_NEXTAUTH" \
  --from-literal=LANGFUSE_INIT_PROJECT_PUBLIC_KEY="$LF_PK" --from-literal=LANGFUSE_INIT_PROJECT_SECRET_KEY="$LF_SK" \
  --from-literal=LANGFUSE_INIT_USER_PASSWORD="$LF_PW" \
  --from-literal=LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID="${OSS_ACCESS_KEY_ID}" \
  --from-literal=LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY="${OSS_ACCESS_KEY_SECRET}" \
  --from-literal=LANGFUSE_S3_MEDIA_UPLOAD_ACCESS_KEY_ID="${OSS_ACCESS_KEY_ID}" \
  --from-literal=LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY="${OSS_ACCESS_KEY_SECRET}" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n atlas-infra create configmap otel-collector \
  --from-file=config.yaml=deploy/server/collector.yaml --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f deploy/server/10-infra.yaml
for sts in postgres redis minio qdrant; do
  kubectl -n atlas-infra rollout status "statefulset/$sts" --timeout=600s
done
kubectl -n atlas-infra wait --for=condition=complete job/minio-init --timeout=300s
# 首次部署之后才加的库：上面的 init SQL 只在数据目录为空时执行，已部署的服务器在这里补建（幂等）
for db in atlas_memory atlas_mcpgateway; do
  if ! kubectl -n atlas-infra exec statefulset/postgres -- psql -U atlas -d atlas -tAc \
       "SELECT 1 FROM pg_database WHERE datname='${db}'" | grep -q 1; then
    kubectl -n atlas-infra exec statefulset/postgres -- psql -U atlas -d atlas -c "CREATE DATABASE ${db}"
  fi
done
[ "${1:-}" != "infra" ] || exit 0

# ── 2. OpenSandbox（CSI 驱动 + 存储 + 控制器 + 两个 lifecycle server）─────────
if [ -z "${OPENSANDBOX_SRC:-}" ]; then
  OPENSANDBOX_SRC="$HOME/.cache/atlas/opensandbox"
  [ -d "$OPENSANDBOX_SRC/manifests" ] || git -c http.proxy="${HTTPS_PROXY:-}" clone --depth 1 \
    --branch release-1.1.1 https://github.com/opensandbox-group/OpenSandbox.git "$OPENSANDBOX_SRC"
fi
OPENSANDBOX_SRC="$OPENSANDBOX_SRC" ATLAS_HOST_IP="$SERVER_IP" ATLAS_OSS_ADDR="minio.${INFRA}:9000" \
  deploy/local/opensandbox.sh

# ── 3. server 的库迁移（server 自己启动时不迁移）─────────────────────────
kubectl -n atlas-system delete pod atlas-migrate --ignore-not-found --wait=true >/dev/null
kubectl -n atlas-system run atlas-migrate --image=atlas-app:dev --image-pull-policy=Never \
  --restart=Never --attach --rm --quiet \
  --env="DATABASE_URL=postgresql+asyncpg://atlas:atlas@postgres.${INFRA}:5432/atlas" \
  --env="REDIS_URL=redis://redis.${INFRA}:6379/0" --env="LITELLM_KEY=unused" \
  -- alembic upgrade head

# ── 4. 后端与前端 ───────────────────────────────────────────────────
ATLAS_HOST_IP="$SERVER_IP" \
ATLAS_PG_ADDR="postgres.${INFRA}:5432" ATLAS_REDIS_ADDR="redis.${INFRA}:6379" \
ATLAS_OSS_ADDR="minio.${INFRA}:9000" ATLAS_QDRANT_ADDR="qdrant.${INFRA}:6333" \
ATLAS_OTEL_ADDR="otel-collector.${INFRA}:4318" ATLAS_HIGRESS_ADDR="higress.${INFRA}:8080" \
ATLAS_PUBLIC_ORIGIN="http://${PUBLIC_HOST}" \
  deploy/local/apply.sh
ATLAS_HOST_IP="$SERVER_IP" deploy/local/frontends.sh --no-build
sed "s/__PUBLIC_HOST__/${SERVER_IP}/g" deploy/server/20-ingress.yaml | kubectl apply -f -

# ── 5. Higress 的 MCP（serpapi，只认 MCP 网关）：经 port-forward 调控制台（控制台不对外）────
kubectl -n atlas-infra rollout status statefulset/higress --timeout=600s
if [ -n "${SERPAPI_KEY:-}" ]; then
  kubectl -n atlas-infra port-forward svc/higress 28001:8001 >/dev/null 2>&1 &
  PF=$!
  trap 'kill $PF 2>/dev/null || true' EXIT
  for _ in $(seq 60); do curl -s -o /dev/null --noproxy '*' http://127.0.0.1:28001 && break; sleep 2; done
  # ★ 只认 MCP 网关（doc/mcp-design.html §8.3）：网关凭据取 apply.sh 建的 Secret，Higress 与 MCP 网关同一把
  MCP_GATEWAY_CREDENTIAL="$(kubectl -n atlas-system get secret atlas-mcpgateway-creds \
    -o jsonpath='{.data.gateway_credential}' | base64 --decode)" \
  HIGRESS_CONSOLE=http://127.0.0.1:28001 python3 deploy/local/higress/setup.py
  kill $PF 2>/dev/null || true
else
  echo "⚠ .env 没有 SERPAPI_KEY：跳过 Higress 的 serpapi MCP"
fi

# ── 6. 用户中心的首个超级管理员（只在还没有任何用户时）────────────────────
if kubectl -n atlas-system exec deploy/atlas-usercenter -- python -m atlas_usercenter bootstrap \
     --company "${UC_COMPANY:-星海科技}" --account admin --name 管理员 --email "${UC_ADMIN_EMAIL:-admin@xinghai.com}" \
     2>/tmp/uc-bootstrap.err; then
  echo "↑ 超级管理员 admin 的临时密码只显示这一次，请记下（首次登录须修改）"
elif grep -q "已经有用户" /tmp/uc-bootstrap.err; then
  echo "用户中心已有用户，跳过初始化"
else
  cat /tmp/uc-bootstrap.err; exit 1
fi

echo
echo "★ 首次部署还差一步：admin 登录 http://${PUBLIC_HOST}/uc/login 改密码后，运行 deploy/server/register.sh"
echo "  （把 Atlas、TeamFlow 登记到用户中心；不做的话两者调用户中心会 403）"
echo "Atlas     → http://${PUBLIC_HOST}/atlas"
echo "用户中心  → http://${PUBLIC_HOST}/uc"
echo "TeamFlow  → http://${PUBLIC_HOST}/teamflow"
echo "Langfuse  → http://langfuse.${SERVER_IP}.nip.io（admin@atlas.local / Secret atlas-infra/langfuse 的 admin_password）"
echo "Jaeger    → kubectl -n atlas-infra port-forward svc/jaeger 16686"
