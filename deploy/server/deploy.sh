#!/usr/bin/env bash
# 把 Atlas 全套部署到服务器（单节点 K8s，默认 192.168.0.112）。全新部署：数据不迁移。
#
#   deploy/server/deploy.sh          全部
#   deploy/server/deploy.sh infra    只部署依赖服务（第 1 步；应用镜像还没导入时可以先做）
#   deploy/server/deploy.sh app      只更新应用（改了代码之后）：server 迁移 → 后端 → 前端 → 登记，
#                                    跳过依赖服务、OpenSandbox、超级管理员初始化
#
# 前置（都在本机；第 0 步逐项检查，缺什么一次列全，在改动任何东西之前退出）：
#   · ssh root@<服务器> 免密；本机有 kubectl、helm（不在 PATH 就用 HELM=… 指定；
#     OpenSandbox 的 chart 与 values 没变时 HELM=true 跳过）
#   · 镜像已导入服务器：deploy/server/images.sh。app、frontends 默认用服务器上最新的构建标签，
#     ATLAS_APP_IMAGE / ATLAS_FRONTENDS_IMAGE 可指定（回退到上一版就指定它的标签）
#   · 仓库根 .env（模型 key、Gitee 令牌、OSS 口令等 —— 与本机部署同一份）；有 UC_ADMIN_PASSWORD
#     （用户中心 admin 的密码）时最后自动登记到用户中心（register.sh），没有就跳过并提示
#   · OpenSandbox 源码 release-1.1.1（OPENSANDBOX_SRC=…；不给就 clone 到 ~/.cache/atlas/opensandbox）
#
# 步骤：前置检查 → 依赖服务（10-infra）→ OpenSandbox（opensandbox.sh）→ server 迁移 → 后端（apply.sh）
#   → 前端（frontends.sh）→ Ingress → 用户中心首个超级管理员（只第一次）
#   → 登记到用户中心（register.sh）
#
# ★ 复用 deploy/local 的清单与脚本：依赖服务的地址经 ATLAS_*_ADDR 换成集群内的服务名（apply.sh 的 render）。
# ★ 幂等：重复执行只更新有变化的部分；随机生成的密钥已存在就沿用。
set -euo pipefail
cd "$(dirname "$0")/../.."

MODE="${1:-all}"
case "$MODE" in all|infra|app) ;; *) echo "用法：deploy/server/deploy.sh [infra|app]"; exit 1 ;; esac
SERVER_IP="${ATLAS_SERVER_IP:-192.168.0.112}"
TARGET="root@${SERVER_IP}"
PUBLIC_HOST="atlas.${SERVER_IP}.nip.io"
# ★ 内网直连，不走开发机代理（本机 shell 里有 HTTPS_PROXY，kubectl 会被它带走）
export NO_PROXY="${NO_PROXY:+${NO_PROXY},}${SERVER_IP}" no_proxy="${no_proxy:+${no_proxy},}${SERVER_IP}"

# ── 0. 前置检查 ─────────────────────────────────────────────────────
# ★ 以前缺 helm 要等依赖服务都 apply 完、拉完 OpenSandbox 源码才在半路报错；缺镜像则是 Pod 起不来才发现
missing=()
for cmd in kubectl ssh openssl curl python3 uv; do
  command -v "$cmd" >/dev/null || missing+=("本机命令 $cmd")
done
if [ "$MODE" = all ] && ! command -v "${HELM:-helm}" >/dev/null; then
  missing+=("helm（OpenSandbox 的 chart 与 values 没变时可以 HELM=true 跳过这一步）")
fi
if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
  for v in OSS_ACCESS_KEY_ID OSS_ACCESS_KEY_SECRET LITELLM_BASE_URL LITELLM_KEY; do
    [ -n "${!v:-}" ] || missing+=(".env 的 $v")
  done
else
  missing+=("仓库根的 .env")
fi
KCFG="${ATLAS_KUBECONFIG:-$HOME/.kube/atlas-${SERVER_IP}.yaml}"
if ssh -o BatchMode=yes -o ConnectTimeout=5 "$TARGET" true 2>/dev/null; then
  if [ ! -s "$KCFG" ]; then
    mkdir -p "$(dirname "$KCFG")"
    ssh "$TARGET" cat /etc/kubernetes/admin.conf > "$KCFG"
    chmod 600 "$KCFG"
  fi
  export KUBECONFIG="$KCFG"
  kubectl get nodes >/dev/null 2>&1 || missing+=("kubectl 连不上 ${SERVER_IP}（kubeconfig：${KCFG}）")
  # 配置服务改为技能应用：库要先改名、持久指令要先搬到 Atlas，否则 atlas-skill 起不来（库不存在）
  if [ "$MODE" != infra ] && kubectl -n atlas-infra exec statefulset/postgres -- psql -U atlas -d atlas -tAc \
       "SELECT 1 FROM pg_database WHERE datname='atlas_config'" 2>/dev/null | grep -q 1; then
    missing+=("库 atlas_config 还在：先跑 deploy/server/skill-cutover.sh（配置服务改为技能应用的一次性切换）")
  fi
  if [ "$MODE" != infra ]; then
    out="$(ATLAS_SERVER="$TARGET" deploy/server/images.sh check 2>&1)" || missing+=("$out")
    # app、frontends：没指定就用服务器上最新的构建标签（images.sh 每次构建打一个新的）
    have="$(ssh "$TARGET" "ctr -n k8s.io images ls -q")"
    latest() { sed -n "s#^docker.io/library/\($1:[0-9]\{8\}-[0-9]\{6\}\)\$#\1#p" <<<"$have" | sort | tail -1; }
    APP_IMAGE="${ATLAS_APP_IMAGE:-$(latest atlas-app)}"
    FRONTENDS_IMAGE="${ATLAS_FRONTENDS_IMAGE:-$(latest atlas-frontends)}"
    for img in "$APP_IMAGE" "$FRONTENDS_IMAGE"; do
      [ -z "$img" ] || grep -qxF "docker.io/library/$img" <<<"$have" || missing+=("服务器上没有指定的镜像 $img")
    done
  fi
else
  missing+=("ssh $TARGET 免密登录")
fi
if [ ${#missing[@]} -gt 0 ]; then
  echo "前置检查没通过，什么都没改："
  printf '  · %s\n' "${missing[@]}"
  exit 1
fi
[ "$MODE" = infra ] || echo "镜像：${APP_IMAGE}、${FRONTENDS_IMAGE}"

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
if [ "$MODE" != app ]; then
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
for db in atlas_memory atlas_mcpgateway atlas_modelgateway atlas_litellm; do
  if ! kubectl -n atlas-infra exec statefulset/postgres -- psql -U atlas -d atlas -tAc \
       "SELECT 1 FROM pg_database WHERE datname='${db}'" | grep -q 1; then
    kubectl -n atlas-infra exec statefulset/postgres -- psql -U atlas -d atlas -c "CREATE DATABASE ${db}"
  fi
done
fi
[ "$MODE" != infra ] || exit 0

# ── 2. OpenSandbox（CSI 驱动 + 存储 + 控制器 + 两个 lifecycle server）─────────
if [ "$MODE" = all ]; then
if [ -z "${OPENSANDBOX_SRC:-}" ]; then
  OPENSANDBOX_SRC="$HOME/.cache/atlas/opensandbox"
  [ -d "$OPENSANDBOX_SRC/manifests" ] || git -c http.proxy="${HTTPS_PROXY:-}" clone --depth 1 \
    --branch release-1.1.1 https://github.com/opensandbox-group/OpenSandbox.git "$OPENSANDBOX_SRC"
fi
OPENSANDBOX_SRC="$OPENSANDBOX_SRC" ATLAS_HOST_IP="$SERVER_IP" ATLAS_OSS_ADDR="minio.${INFRA}:9000" \
  deploy/local/opensandbox.sh
fi

# ── 3. server 的库迁移（server 自己启动时不迁移）─────────────────────────
kubectl -n atlas-system delete pod atlas-migrate --ignore-not-found --wait=true >/dev/null
kubectl -n atlas-system run atlas-migrate --image="$APP_IMAGE" --image-pull-policy=Never \
  --restart=Never --attach --rm --quiet \
  --env="DATABASE_URL=postgresql+asyncpg://atlas:atlas@postgres.${INFRA}:5432/atlas" \
  --env="REDIS_URL=redis://redis.${INFRA}:6379/0" --env="LITELLM_KEY=unused" \
  -- alembic upgrade head

# ── 4. 后端与前端 ───────────────────────────────────────────────────
ATLAS_HOST_IP="$SERVER_IP" \
ATLAS_PG_ADDR="postgres.${INFRA}:5432" ATLAS_REDIS_ADDR="redis.${INFRA}:6379" \
ATLAS_OSS_ADDR="minio.${INFRA}:9000" ATLAS_QDRANT_ADDR="qdrant.${INFRA}:6333" \
ATLAS_OTEL_ADDR="otel-collector.${INFRA}:4318" \
ATLAS_PUBLIC_ORIGIN="http://${PUBLIC_HOST}" ATLAS_APP_IMAGE="$APP_IMAGE" \
  deploy/local/apply.sh
ATLAS_HOST_IP="$SERVER_IP" ATLAS_FRONTENDS_IMAGE="$FRONTENDS_IMAGE" deploy/local/frontends.sh --no-build
sed "s/__PUBLIC_HOST__/${SERVER_IP}/g" deploy/server/20-ingress.yaml | kubectl apply -f -

# ── 5. 用户中心的首个超级管理员（只在还没有任何用户时）────────────────────
if [ "$MODE" = app ]; then
  :
elif kubectl -n atlas-system exec deploy/atlas-usercenter -- python -m atlas_usercenter bootstrap \
     --company "${UC_COMPANY:-星海科技}" --account admin --name 管理员 --email "${UC_ADMIN_EMAIL:-admin@xinghai.com}" \
     2>/tmp/uc-bootstrap.err; then
  echo "↑ 超级管理员 admin 的临时密码只显示这一次，请记下（首次登录须修改）"
elif grep -q "已经有用户" /tmp/uc-bootstrap.err; then
  echo "用户中心已有用户，跳过初始化"
else
  cat /tmp/uc-bootstrap.err; exit 1
fi

# ── 6. 登记到用户中心（幂等：应用、操作码、角色、数据编码随代码变化，每次部署都同步一遍）──────
# ★ 首次部署时 admin 还是临时密码，register-uc 会拒绝：登录改密码后单独跑 register.sh
if [ -z "${UC_ADMIN_PASSWORD:-}" ]; then
  echo "⚠ .env 没有 UC_ADMIN_PASSWORD：跳过登记到用户中心，需要时手动运行 deploy/server/register.sh"
elif ! deploy/server/register.sh </dev/null; then
  echo "⚠ 登记到用户中心失败（见上）。首次部署要先用 admin 登录改密码，再运行 deploy/server/register.sh"
fi

echo
echo "★ 首次部署：admin 登录 http://${PUBLIC_HOST}/uc/login 改密码后，运行 deploy/server/register.sh"
echo "  （把 Atlas、TeamFlow 登记到用户中心；不做的话两者调用户中心会 403）"
echo "Atlas     → http://${PUBLIC_HOST}/atlas"
echo "用户中心  → http://${PUBLIC_HOST}/uc"
echo "TeamFlow  → http://${PUBLIC_HOST}/teamflow"
echo "Langfuse  → http://langfuse.${SERVER_IP}.nip.io（admin@atlas.local / Secret atlas-infra/langfuse 的 admin_password）"
echo "Higress   → http://higress.${SERVER_IP}.nip.io（控制台；账号见 deploy/local/higress/setup.py）"
echo "Jaeger    → kubectl -n atlas-infra port-forward svc/jaeger 16686"
