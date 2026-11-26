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
#   · 也可以在服务器上直接运行（git clone 到服务器）：本机有 /etc/kubernetes/admin.conf 就当作在服务器上，
#     不经 ssh（ATLAS_SERVER=local 同义；集群用 deploy/server/cluster-setup.sh 搭）
#   · 镜像已导入服务器：deploy/server/images.sh。app、frontends 默认用服务器上最新的构建标签，
#     ATLAS_APP_IMAGE / ATLAS_FRONTENDS_IMAGE 可指定（回退到上一版就指定它的标签）
#   · 仓库根 .env（模型 key、Gitee 令牌、OSS 口令等 —— 与本机部署同一份）。
#     部署参数也可以写在 .env 里（都有默认值）：
#       ATLAS_SERVER_IP       服务器 IP（默认 192.168.0.112）
#       ATLAS_PUBLIC_HOST     对外域名（默认 atlas.<服务器 IP>.nip.io）；ATLAS_PUBLIC_SCHEME 默认 https
#                             （对外的 80 / 443 与证书归宿主机上的 API 网关：deploy/server/gateway.sh）
#       ATLAS_LANGFUSE_HOST   Langfuse 的域名（默认 langfuse.<服务器 IP>.nip.io；API 网关的透传站点）
#       ATLAS_OBSERVABILITY   full（默认）| none：none 不装 Jaeger、Langfuse（11-observability.yaml），小机器用
#       TENANT_BACKFILL_UUID  多租户迁移把已有数据归到哪个租户（用户中心里原平台租户的 uuid）；
#                             库里已有数据又没给，各服务的迁移会失败。新部署、已迁移过的不需要
#   · OpenSandbox 源码 release-1.1.1（OPENSANDBOX_SRC=…；不给就 clone 到 ~/.cache/atlas/opensandbox）
#   · API 网关已装在服务器上（deploy/server/gateway.sh install、cert）；从 Traefik 对外换过来的第一次走
#     deploy/server/apigateway-cutover.sh
#
# 步骤：前置检查 → 依赖服务（10-infra）→ OpenSandbox（opensandbox.sh）→ server 迁移 → 后端（apply.sh）
#   → 前端（frontends.sh）→ API 网关重新渲染（在服务器上跑时）→ 用户中心首个超级管理员、系统租户（只第一次）
#
# ★ 复用 deploy/local 的清单与脚本：依赖服务的地址经 ATLAS_*_ADDR 换成集群内的服务名（apply.sh 的 render）。
# ★ 幂等：重复执行只更新有变化的部分；随机生成的密钥已存在就沿用。
set -euo pipefail
cd "$(dirname "$0")/../.."

MODE="${1:-all}"
case "$MODE" in all|infra|app) ;; *) echo "用法：deploy/server/deploy.sh [infra|app]"; exit 1 ;; esac
# 部署参数可以写在 .env 里（见上），先读进来
if [ -f .env ]; then set -a; . ./.env; set +a; fi
SERVER_IP="${ATLAS_SERVER_IP:-192.168.0.112}"
# 在服务器上直接运行：本机就是 K8s 控制面节点 → 命令就地执行，不经 ssh
if [ -z "${ATLAS_SERVER:-}" ] && [ -r /etc/kubernetes/admin.conf ]; then ATLAS_SERVER=local; fi
TARGET="${ATLAS_SERVER:-root@${SERVER_IP}}"
on_server() { if [ "$TARGET" = local ]; then bash -c "$1"; else ssh "$TARGET" "$1"; fi; }
PUBLIC_HOST="${ATLAS_PUBLIC_HOST:-atlas.${SERVER_IP}.nip.io}"
PUBLIC_SCHEME="${ATLAS_PUBLIC_SCHEME:-https}"
LANGFUSE_HOST="${ATLAS_LANGFUSE_HOST:-langfuse.${SERVER_IP}.nip.io}"
OBSERVABILITY="${ATLAS_OBSERVABILITY:-full}"
case "$OBSERVABILITY" in full|none) ;; *) echo "ATLAS_OBSERVABILITY 只能是 full 或 none"; exit 1 ;; esac
# ★ 内网直连，不走开发机代理（本机 shell 里有 HTTPS_PROXY，kubectl 会被它带走）
export NO_PROXY="${NO_PROXY:+${NO_PROXY},}${SERVER_IP}" no_proxy="${no_proxy:+${no_proxy},}${SERVER_IP}"

# ── 0. 前置检查 ─────────────────────────────────────────────────────
# ★ 以前缺 helm 要等依赖服务都 apply 完、拉完 OpenSandbox 源码才在半路报错；缺镜像则是 Pod 起不来才发现
missing=()
for cmd in kubectl ssh openssl curl python3; do
  command -v "$cmd" >/dev/null || missing+=("本机命令 $cmd")
done
if [ "$MODE" = all ] && ! command -v "${HELM:-helm}" >/dev/null; then
  missing+=("helm（OpenSandbox 的 chart 与 values 没变时可以 HELM=true 跳过这一步）")
fi
if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
  for v in OSS_ACCESS_KEY_ID OSS_ACCESS_KEY_SECRET; do
    [ -n "${!v:-}" ] || missing+=(".env 的 $v")
  done
else
  missing+=("仓库根的 .env")
fi
KCFG="${ATLAS_KUBECONFIG:-$HOME/.kube/atlas-${SERVER_IP}.yaml}"
[ "$TARGET" != local ] || KCFG="${ATLAS_KUBECONFIG:-/etc/kubernetes/admin.conf}"
if [ "$TARGET" = local ] || ssh -o BatchMode=yes -o ConnectTimeout=5 "$TARGET" true 2>/dev/null; then
  if [ ! -s "$KCFG" ]; then
    mkdir -p "$(dirname "$KCFG")"
    ssh "$TARGET" cat /etc/kubernetes/admin.conf > "$KCFG"
    chmod 600 "$KCFG"
  fi
  export KUBECONFIG="$KCFG" ATLAS_KUBECONFIG="$KCFG"
  kubectl get nodes >/dev/null 2>&1 || missing+=("kubectl 连不上 ${SERVER_IP}（kubeconfig：${KCFG}）")
  # 配置服务改为技能应用：库要先改名、持久指令要先搬到 Atlas，否则 atlas-skill 起不来（库不存在）
  if [ "$MODE" != infra ] && kubectl -n atlas-infra exec statefulset/postgres -- psql -U atlas -d atlas -tAc \
       "SELECT 1 FROM pg_database WHERE datname='atlas_config'" 2>/dev/null | grep -q 1; then
    missing+=("库 atlas_config 还在：先跑 deploy/server/skill-cutover.sh（配置服务改为技能应用的一次性切换）")
  fi
  if [ "$MODE" != infra ]; then
    out="$(ATLAS_SERVER="$TARGET" ATLAS_OBSERVABILITY="$OBSERVABILITY" deploy/server/images.sh check 2>&1)" \
      || missing+=("$out")
    # app、frontends：没指定就用服务器上最新的构建标签（images.sh 每次构建打一个新的）
    have="$(on_server "ctr -n k8s.io images ls -q")"
    latest() { sed -n "s#^docker.io/library/\($1:[0-9]\{8\}-[0-9]\{6\}\)\$#\1#p" <<<"$have" | sort | tail -1; }
    APP_IMAGE="${ATLAS_APP_IMAGE:-$(latest atlas-app)}"
    FRONTENDS_IMAGE="${ATLAS_FRONTENDS_IMAGE:-$(latest atlas-frontends)}"
    for img in "$APP_IMAGE" "$FRONTENDS_IMAGE"; do
      [ -z "$img" ] || grep -qxF "docker.io/library/$img" <<<"$have" || missing+=("服务器上没有指定的镜像 $img")
    done
    # 对外入口是宿主机上的 API 网关：没装就部署，前端的新路径（/<名>-ui）从外面一个都打不开
    if [ "${ATLAS_GATEWAY_CUTOVER:-}" != 1 ] && ! on_server "test -x /usr/local/openresty/nginx/sbin/nginx"; then
      missing+=("服务器上没装 API 网关：第一次从 Traefik 对外换过来，跑 deploy/server/apigateway-cutover.sh")
    fi
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
  --from-literal=NEXTAUTH_URL="${PUBLIC_SCHEME}://${LANGFUSE_HOST}" --from-literal=NEXTAUTH_SECRET="$LF_NEXTAUTH" \
  --from-literal=LANGFUSE_INIT_PROJECT_PUBLIC_KEY="$LF_PK" --from-literal=LANGFUSE_INIT_PROJECT_SECRET_KEY="$LF_SK" \
  --from-literal=LANGFUSE_INIT_USER_PASSWORD="$LF_PW" \
  --from-literal=LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID="${OSS_ACCESS_KEY_ID}" \
  --from-literal=LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY="${OSS_ACCESS_KEY_SECRET}" \
  --from-literal=LANGFUSE_S3_MEDIA_UPLOAD_ACCESS_KEY_ID="${OSS_ACCESS_KEY_ID}" \
  --from-literal=LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY="${OSS_ACCESS_KEY_SECRET}" \
  --dry-run=client -o yaml | kubectl apply -f -
# 不装 Jaeger、Langfuse 时 collector 照收、直接丢弃（应用清单不用改）
COLLECTOR_CONFIG=deploy/server/collector.yaml
[ "$OBSERVABILITY" = full ] || COLLECTOR_CONFIG=deploy/server/collector-none.yaml
kubectl -n atlas-infra create configmap otel-collector \
  --from-file=config.yaml="$COLLECTOR_CONFIG" --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f deploy/server/10-infra.yaml
if [ "$OBSERVABILITY" = full ]; then
  kubectl apply -f deploy/server/11-observability.yaml
fi
for sts in postgres redis minio qdrant; do
  kubectl -n atlas-infra rollout status "statefulset/$sts" --timeout=600s
done
kubectl -n atlas-infra wait --for=condition=complete job/minio-init --timeout=300s
fi
# 首次部署之后才加的库：10-infra 的 init SQL 只在数据目录为空时执行，已部署的服务器在这里补建
# （幂等；app 模式也跑 —— 新服务的库要在它的 Deployment 迁移之前就有）
for db in atlas_memory atlas_mcpgateway atlas_modelgateway atlas_opscenter; do
  if ! kubectl -n atlas-infra exec statefulset/postgres -- psql -U atlas -d atlas -tAc \
       "SELECT 1 FROM pg_database WHERE datname='${db}'" | grep -q 1; then
    kubectl -n atlas-infra exec statefulset/postgres -- psql -U atlas -d atlas -c "CREATE DATABASE ${db}"
  fi
done
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

# ── 3. server 的库迁移（server 自己启动时不迁移；应用配置库的表跟在后面）──────
kubectl -n atlas-system delete pod atlas-migrate --ignore-not-found --wait=true >/dev/null
kubectl -n atlas-system run atlas-migrate --image="$APP_IMAGE" --image-pull-policy=Never \
  --restart=Never --attach --rm --quiet \
  --env="DATABASE_URL=postgresql+asyncpg://atlas:atlas@postgres.${INFRA}:5432/atlas" \
  --env="REDIS_URL=redis://redis.${INFRA}:6379/0" \
  --env="TENANT_BACKFILL_UUID=${TENANT_BACKFILL_UUID:-}" \
  -- sh -c 'alembic upgrade head && python -m atlas_agent migrate-appconfig'

# ── 4. 后端与前端 ───────────────────────────────────────────────────
ATLAS_HOST_IP="$SERVER_IP" \
ATLAS_PG_ADDR="postgres.${INFRA}:5432" ATLAS_REDIS_ADDR="redis.${INFRA}:6379" \
ATLAS_OSS_ADDR="minio.${INFRA}:9000" ATLAS_QDRANT_ADDR="qdrant.${INFRA}:6333" \
ATLAS_OTEL_ADDR="otel-collector.${INFRA}:4318" \
ATLAS_PUBLIC_ORIGIN="${PUBLIC_SCHEME}://${PUBLIC_HOST}" ATLAS_APP_IMAGE="$APP_IMAGE" \
  deploy/local/apply.sh
# LiteLLM 已退役（apply.sh 删了它的 Deployment）：它的库也删掉。FORCE 断开残留连接（幂等）
kubectl -n atlas-infra exec statefulset/postgres -- psql -U atlas -d atlas -qc \
  "DROP DATABASE IF EXISTS atlas_litellm WITH (FORCE)"
ATLAS_FRONTENDS_IMAGE="$FRONTENDS_IMAGE" deploy/local/frontends.sh --no-build
# 以前的对外入口（Traefik 按域名转给前端 Pod 里的 nginx）：对外改由 API 网关，删掉（幂等）
kubectl -n atlas-system delete ingress atlas --ignore-not-found
kubectl -n atlas-system delete configmap atlas-frontends-nginx --ignore-not-found
# API 网关：路由表（infra-platform/gateway/routes.yaml）随代码变，重新渲染并 reload（配置不过不切）。
#   它在宿主机上、要 root，只有在服务器上跑本脚本时能顺带做；切换期间由 apigateway-cutover.sh 做
if [ "${ATLAS_GATEWAY_CUTOVER:-}" = 1 ]; then
  :
elif [ "$TARGET" = local ]; then
  LF_SITE=""; [ "$OBSERVABILITY" = full ] && LF_SITE="$LANGFUSE_HOST"
  ATLAS_PUBLIC_HOST="$PUBLIC_HOST" ATLAS_LANGFUSE_HOST="$LF_SITE" deploy/server/gateway.sh
else
  echo "★ routes.yaml 有改动时，在服务器的仓库里以 root 跑 deploy/server/gateway.sh"
fi

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
# 系统租户（运营团队；运营中心只开给它，c4-model/opscenter/design-opscenter.html）：只能有一个，已有就跳过。
#   运营中心起来后自己登记应用目录、给系统租户开通；运营人员在用户中心里授运营模块
if kubectl -n atlas-system exec deploy/atlas-usercenter -- python -m atlas_usercenter bootstrap-system \
     --code "${UC_SYSTEM_TENANT:-platform}" --company "${UC_SYSTEM_COMPANY:-平台运营}" --account admin \
     --name 运营管理员 --email "${UC_SYSTEM_EMAIL:-ops@xinghai.com}" 2>/tmp/uc-system.err; then
  echo "↑ 系统租户 ${UC_SYSTEM_TENANT:-platform} 的 admin 临时密码只显示这一次，请记下（首次登录须修改）"
elif grep -q "已经有系统租户" /tmp/uc-system.err; then
  echo "已有系统租户，跳过"
else
  cat /tmp/uc-system.err; exit 1
fi

# ★ 应用目录（应用、操作码、菜单、数据编码、内置角色）不由代码登记：运营人员在运营中心录入
#   （各服务 README「用户中心登记」）。没录之前各应用调用户中心会 403
echo
echo "★ 首次部署：admin 登录 ${PUBLIC_SCHEME}://${PUBLIC_HOST}/uc-ui/login 改密码；应用目录在运营中心录入"
echo "Atlas     → ${PUBLIC_SCHEME}://${PUBLIC_HOST}/atlas-ui/"
echo "用户中心  → ${PUBLIC_SCHEME}://${PUBLIC_HOST}/uc-ui/"
echo "TeamFlow  → ${PUBLIC_SCHEME}://${PUBLIC_HOST}/teamflow-ui/"
echo "运营中心  → ${PUBLIC_SCHEME}://${PUBLIC_HOST}/opscenter-ui/（系统租户的人登录）"
if [ "$OBSERVABILITY" = full ]; then
  echo "Langfuse  → ${PUBLIC_SCHEME}://${LANGFUSE_HOST}（admin@atlas.local / Secret atlas-infra/langfuse 的 admin_password）"
  echo "Jaeger    → kubectl -n atlas-infra port-forward svc/jaeger 16686"
fi
