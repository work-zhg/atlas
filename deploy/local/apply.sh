#!/usr/bin/env bash
# 把用户中心、server、技能应用、knowledge、memory、MCP 网关、模型网关、设备网关、TeamFlow 后端与运营中心后端部署到本机 K8s（Docker Desktop）。
#
# 前置：make up（宿主的 PG / Redis / MinIO）、镜像已构建：
#   docker build -f docker/bridge/Dockerfile -t atlas-cli-bridge:0.4.0 .   # CLI 会话的 CLI 镜像
#   docker/app/requirements.sh                                             # 按 uv.lock 导出第三方依赖
#   docker build -f docker/app/Dockerfile    -t atlas-app:dev .
# 会话 Pod（原生沙箱、ACP）由 OpenSandbox 创建：先跑 deploy/local/opensandbox.sh。
#
# ★ 清单里写的是 atlas-app:dev；ATLAS_APP_IMAGE 给了就换成它（服务器部署每次构建一个新标签，
#   清单变了 apply 才会滚动更新，见 deploy/server/images.sh）。本机照旧用 :dev。
#
# ★ 凭据从仓库根的 .env 读，建成 K8s Secret —— 不进任何 YAML，
#   因为 YAML 会进 git。
set -euo pipefail
cd "$(dirname "$0")/../.."

[ -f .env ] || { echo "缺 .env"; exit 1; }
# shellcheck disable=SC1091
set -a; . ./.env; set +a

kubectl apply -f deploy/local/00-namespaces.yaml

# 沙箱的网络隔离（08-sandbox-network.yaml）：出站到集群外的放行要除掉 Pod 网段。
# 探测同 egress-rules.sh：有 Calico 读它的 IP 池（不按节点的 podCIDR 分配），没有用各节点的 podCIDR；
# 也可以用 SANDBOX_POD_CIDRS="10.42.0.0/16" 手动指定
pod_cidrs="${SANDBOX_POD_CIDRS:-}"
if [ -z "$pod_cidrs" ]; then
  pod_cidrs="$(kubectl get ippools.crd.projectcalico.org \
    -o jsonpath='{range .items[*]}{.spec.cidr}{"\n"}{end}' 2>/dev/null || true)"
  [ -n "$pod_cidrs" ] || pod_cidrs="$(kubectl get nodes \
    -o jsonpath='{range .items[*]}{.spec.podCIDRs[*]}{"\n"}{end}' | tr ' ' '\n')"
fi
pod_cidrs="$(printf '%s\n' $pod_cidrs | sed '/^$/d' | sort -u | paste -sd, -)"
[ -n "$pod_cidrs" ] || { echo "取不到 Pod 网段，用 SANDBOX_POD_CIDRS 指定" >&2; exit 1; }
sed "s#__POD_CIDRS__#${pod_cidrs}#" deploy/local/08-sandbox-network.yaml | kubectl apply -f -

# ★ create --dry-run=client | apply：幂等。直接 create 第二次会 AlreadyExists，
#   而 apply -f 一个 Secret 字面量又会把凭据写进临时文件。
kubectl -n atlas-system create secret generic atlas-server-creds \
  --from-literal=oss_access_key_id="${OSS_ACCESS_KEY_ID}" \
  --from-literal=oss_access_key_secret="${OSS_ACCESS_KEY_SECRET}" \
  --from-literal=cli_model_base_url="${ATLAS_CLI_MODEL_BASE_URL:-${ATLAS_ACP_MODEL_BASE_URL:-${ATLAS_CLUSTER_MODEL_BASE_URL:-}}}" \
  --dry-run=client -o yaml | kubectl apply -f -

# 身份凭据（X-Identity-Grant）的签名密钥：Atlas 打开 CLI 会话时签、MCP 网关验，网关据此给上游写
# 这一轮的 auth-uid（知识库按它判权限）。.env 里有就用；没有就沿用集群里已有的；都没有才新生成。
GRANT_SECRET="${ATLAS_MCP_IDENTITY_GRANT_SECRET:-$(kubectl -n atlas-system get secret atlas-mcp-identity-grant \
  -o jsonpath='{.data.secret}' 2>/dev/null | base64 --decode || true)}"
[ -n "$GRANT_SECRET" ] || GRANT_SECRET="$(openssl rand -hex 24)"
kubectl -n atlas-system create secret generic atlas-mcp-identity-grant \
  --from-literal=secret="${GRANT_SECRET}" \
  --dry-run=client -o yaml | kubectl apply -f -
# 知识库不再开 MCP 端点（CLI 经 MCP 网关上的平台服务调检索接口）：它的令牌密钥不要了
kubectl -n atlas-system delete secret atlas-knowledge-mcp --ignore-not-found

# 旧版 MCP 网关的凭据：网关直连上游、上游凭据存在网关库里之后不再需要
kubectl -n atlas-system delete secret atlas-mcpgateway-creds --ignore-not-found

secret_value() { kubectl -n atlas-system get secret "$1" -o jsonpath="{.data.$2}" 2>/dev/null | base64 --decode || true; }
# 上一版自研模型网关的凭据（已不用；现在的是下面的 atlas-modelgw-creds）
kubectl -n atlas-system delete secret atlas-modelgateway-creds --ignore-not-found
# LiteLLM 已退役：模型调用全部经自研模型网关（38-modelgateway.yaml）直连供应商。
# ★ 删之前先在模型管理端把经 LiteLLM 的线路换成直连账号，否则这些模型就没有线路了。
#   库 atlas_litellm 由 deploy/server/deploy.sh 删（本机的库在宿主 PG 上，要删手动 DROP）
kubectl -n atlas-system delete deployment/atlas-litellm service/atlas-litellm secret/atlas-litellm-creds \
  --ignore-not-found

# 运营接口（/ops/v1）的签名密钥：每个系统一把，各系统的 ops_api_key 取自己那项，运营中心（48-opscenter.yaml）
# 取全部（system_keys，JSON）。沿用集群里已有的，没有才新生成；不放 .env。
# ★ 换某个系统的密钥：删掉 Secret 里那一项再跑本脚本，然后滚动重启那个系统和运营中心
OPS_ARGS=()
OPS_JSON=""
for sys_code in usercenter atlas knowledge skill mcpgateway modelgateway memory teamflow; do
  key="$(secret_value atlas-ops-keys "$sys_code")"
  [ -n "$key" ] || key="$(openssl rand -hex 32)"
  OPS_ARGS+=(--from-literal="${sys_code}=${key}")
  OPS_JSON="${OPS_JSON:+${OPS_JSON},}\"${sys_code}\":\"${key}\""
done
kubectl -n atlas-system create secret generic atlas-ops-keys "${OPS_ARGS[@]}" \
  --from-literal=system_keys="{${OPS_JSON}}" \
  --dry-run=client -o yaml | kubectl apply -f -

# 模型管理平台 + 模型网关（38-modelgateway.yaml）：账号密钥的主密钥（Fernet，逗号分隔、第一把加密）、
# 海外供应商的出网代理。.env 里有就用；没有就沿用集群里已有的；主密钥都没有才新生成。
# ★ 主密钥设了就不能丢：丢了库里存的账号密钥就解不开。轮换是在前面加一把新的、旧的留着
MODELGW_KEYS="${ATLAS_MODELGW_SECRET_KEYS:-$(secret_value atlas-modelgw-creds secret_keys)}"
[ -n "$MODELGW_KEYS" ] || MODELGW_KEYS="$(openssl rand -base64 32 | tr '+/' '-_')"
kubectl -n atlas-system create secret generic atlas-modelgw-creds \
  --from-literal=secret_keys="${MODELGW_KEYS}" \
  --from-literal=egress_proxy="${ATLAS_MODELGW_EGRESS_PROXY:-$(secret_value atlas-modelgw-creds egress_proxy)}" \
  --dry-run=client -o yaml | kubectl apply -f -

# 多租户迁移把已有数据归到原平台租户（用户中心里的租户 uuid）。只在 .env 给了时建；
# 已经迁移过的库不再需要它
if [ -n "${TENANT_BACKFILL_UUID:-}" ]; then
  kubectl -n atlas-system create configmap atlas-tenancy \
    --from-literal=backfill_uuid="${TENANT_BACKFILL_UUID}" \
    --dry-run=client -o yaml | kubectl apply -f -
fi

# 用户中心签访问 JWT 的私钥（Ed25519，c4-model/apigateway/code-auth.html）：沿用集群里已有的，没有才新生成。
# ★ 丢了不要紧（只是所有人的访问令牌失效、前端自动刷新），但不要随便换：换钥要先发布新公钥，
#   见 usercenter 的 jwt_published_keys 说明。published_keys 是换钥时额外发布的公钥（JSON，平时为空）
UC_JWT_KEY="$(secret_value atlas-uc-jwt signing_key)"
[ -n "$UC_JWT_KEY" ] || UC_JWT_KEY="$(openssl genpkey -algorithm ed25519)"
UC_JWT_PUBLISHED="$(secret_value atlas-uc-jwt published_keys)"
[ -n "$UC_JWT_PUBLISHED" ] || UC_JWT_PUBLISHED='{}'
kubectl -n atlas-system create secret generic atlas-uc-jwt \
  --from-literal=signing_key="${UC_JWT_KEY}" \
  --from-literal=published_keys="${UC_JWT_PUBLISHED}" \
  --dry-run=client -o yaml | kubectl apply -f -

# 设备网关的内部令牌（atlas-server 连会话流、调 /internal 时带上）：沿用集群里已有的，没有才新生成
DEVICEGW_TOKEN="$(secret_value atlas-devicegw internal_token)"
[ -n "$DEVICEGW_TOKEN" ] || DEVICEGW_TOKEN="$(openssl rand -hex 24)"
kubectl -n atlas-system create secret generic atlas-devicegw \
  --from-literal=internal_token="${DEVICEGW_TOKEN}" \
  --dry-run=client -o yaml | kubectl apply -f -

# 沙箱浏览器（c4-model/agent/browser/design-browser.html）：登录态的加密密钥（{版本: base64 32 字节}）、
# 画面口令的派生密钥。沿用集群里已有的，没有才新生成。
# ★ 登录态密钥设了就不能丢：丢了库里存的登录态就解不开。换钥匙是加一个新版本、旧的留着
BROWSER_KEYS="$(secret_value atlas-browser state_keys)"
[ -n "$BROWSER_KEYS" ] || BROWSER_KEYS="{\"1\": \"$(openssl rand -base64 32)\"}"
BROWSER_VIEW="$(secret_value atlas-browser view_secret)"
[ -n "$BROWSER_VIEW" ] || BROWSER_VIEW="$(openssl rand -hex 24)"
kubectl -n atlas-system create secret generic atlas-browser \
  --from-literal=state_keys="${BROWSER_KEYS}" \
  --from-literal=view_secret="${BROWSER_VIEW}" \
  --dry-run=client -o yaml | kubectl apply -f -

# ★ 内网免令牌（doc/identity-propagation-design.html）：服务间令牌、用户中心 App Key / Secret、
#   Atlas 服务账号密钥都已下线，这里不再建。
# 对象存储的 HMAC：技能应用与知识库共用（原名 atlas-config-creds，配置服务改为技能应用时改名）
kubectl -n atlas-system create secret generic atlas-oss-creds \
  --from-literal=oss_access_key_id="${OSS_ACCESS_KEY_ID}" \
  --from-literal=oss_access_key_secret="${OSS_ACCESS_KEY_SECRET}" \
  --dry-run=client -o yaml | kubectl apply -f -

# TeamFlow 后端不再有自己的凭据（产出物在流程空间里，不接 git）：删掉旧的 Gitee 令牌
kubectl -n atlas-system delete secret atlas-teamflow-creds --ignore-not-found

# TeamFlow 的 git 凭据档案（会话 Pod 里 git push 产物，经 Credential Vault）：.env 没有 Gitee 令牌就跳过
if [ -n "${ATLAS_GIT_TEAMFLOW_TOKEN:-${ATLAS_TF_GITEE_TOKEN:-}}" ]; then
  deploy/local/credential-profiles.sh
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
PUBLIC_ORIGIN="${ATLAS_PUBLIC_ORIGIN:-http://localhost:8080}"
# 对外是 HTTPS 时登录 Cookie 加 Secure（用户中心）
case "$PUBLIC_ORIGIN" in https://*) PUBLIC_HTTPS=true ;; *) PUBLIC_HTTPS=false ;; esac
APP_IMAGE="${ATLAS_APP_IMAGE:-atlas-app:dev}"
render() {
  sed -e "s#image: atlas-app:dev\$#image: ${APP_IMAGE}#" -e "s#__HOST_IP__:5433#${PG_ADDR}#g" -e "s#__HOST_IP__:6380#${REDIS_ADDR}#g" \
      -e "s#__HOST_IP__:9000#${OSS_ADDR}#g" -e "s#__HOST_IP__:6333#${QDRANT_ADDR}#g" \
      -e "s#__HOST_IP__:4318#${OTEL_ADDR}#g" \
      -e "s#__PUBLIC_ORIGIN__#${PUBLIC_ORIGIN}#g" -e "s#__PUBLIC_HTTPS__#${PUBLIC_HTTPS}#g" \
      -e "s#__HOST_IP__#${HOST_IP}#g" "$1"
}

# 一次性（内网身份传递 §4）：后端 Service 从 LoadBalancer 改为 ClusterIP，只经网关对外。
# 直接 apply 改不了类型（已分配的 nodePort 会被拒），先删旧的再建 —— 只影响这次 apply 的几秒。
for svc in atlas-usercenter atlas-skill atlas-server atlas-teamflow; do
  if [ "$(kubectl -n atlas-system get svc "$svc" -o jsonpath='{.spec.type}' 2>/dev/null)" = "LoadBalancer" ]; then
    kubectl -n atlas-system delete svc "$svc"
  fi
done

# 用户中心先起：server / knowledge / TeamFlow 都依赖它
render deploy/local/15-usercenter.yaml | kubectl apply -f -
kubectl -n atlas-system rollout status deploy/atlas-usercenter --timeout=180s
render deploy/local/25-skill.yaml   | kubectl apply -f -
render deploy/local/36-memory.yaml  | kubectl apply -f -
render deploy/local/37-mcpgateway.yaml | kubectl apply -f -
render deploy/local/38-modelgateway.yaml | kubectl apply -f -
render deploy/local/30-server.yaml  | kubectl apply -f -
render deploy/local/32-devicegw.yaml | kubectl apply -f -
render deploy/local/35-knowledge.yaml | kubectl apply -f -
render deploy/local/45-teamflow.yaml | kubectl apply -f -
render deploy/local/48-opscenter.yaml | kubectl apply -f -

kubectl -n atlas-system rollout status deploy/atlas-skill --timeout=180s
kubectl -n atlas-system rollout status deploy/atlas-memory --timeout=180s
kubectl -n atlas-system rollout status deploy/atlas-mcpgateway --timeout=180s
kubectl -n atlas-system rollout status deploy/atlas-modelgateway --timeout=180s
kubectl -n atlas-system rollout status deploy/atlas-llmgateway --timeout=300s
kubectl -n atlas-system rollout status deploy/atlas-server --timeout=180s
kubectl -n atlas-system rollout status deploy/atlas-devicegw --timeout=180s
kubectl -n atlas-system rollout status deploy/atlas-knowledge --timeout=300s
kubectl -n atlas-system rollout status deploy/atlas-knowledge-worker --timeout=300s
kubectl -n atlas-system rollout status deploy/atlas-teamflow --timeout=180s
kubectl -n atlas-system rollout status deploy/atlas-opscenter --timeout=180s
# 配置服务改为技能应用（atlas-skill）之后的遗留：旧 Deployment / Service 与改名前的 Secret。
# ★ 放在最后：知识库的新 Pod 已经改用 atlas-oss-creds，删旧 Secret 不会挡住它们启动。
#   服务器上第一次切换走 deploy/server/skill-cutover.sh（先停旧服务、改库名、搬持久指令）
kubectl -n atlas-system delete deployment/atlas-config service/atlas-config secret/atlas-config-creds \
  --ignore-not-found
echo "后端只经 API 网关对外（infra-platform/gateway，宿主机上的 OpenResty）；"
echo "运维直连用 kubectl -n atlas-system port-forward svc/<服务> <端口>（评测见 forward.sh）"
