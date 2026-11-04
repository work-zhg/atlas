#!/usr/bin/env bash
# 本机 K8s（Docker Desktop）上的 OpenSandbox（doc/sandbox-opensandbox-design.html §17）：
#   S3 CSI 驱动 + 整桶静态 PV / PVC → CRD + 控制器 → 两个 lifecycle server：
#   opensandbox-sbx（原生沙箱，命名空间 atlas-sbx）、opensandbox-cli（会话 Pod，命名空间 atlas-cli）。
#   OPENSANDBOX_BENCH=1 时再装第三个：opensandbox-bench（评测档：root、根可写，也建在 atlas-sbx），
#   放行的镜像前缀用 OPENSANDBOX_BENCH_IMAGE_PREFIXES（JSON 数组，默认 ["alexgshaw/"] = Terminal-Bench 2）。
#
# 前置：
#   · make up（宿主的 MinIO）；仓库根 .env 里有 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET，
#     以及 会话 Pod 的模型 key ATLAS_ACP_MODEL_API_KEY（没有就退回旧名 ATLAS_CLUSTER_MODEL_API_KEY）
#   · 镜像 atlas-csi-s3:0.43.7（docker/csi-s3/Dockerfile）、atlas-sandbox:dev（docker/sandbox/Dockerfile）、
#     atlas-egress:release-1.1.1（原生沙箱的 egress sidecar：deploy/local/egress-rules.sh 生成拒绝集群网段的
#     deny.always，再 docker build -t atlas-egress:release-1.1.1 docker/egress）
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
kubectl apply -f deploy/local/07-cli-storage.yaml

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

# 沙箱模板的种子（迁移 0035 也从这里写进 sandbox_template）
SEED=agent-platform/services/agent/src/atlas_agent/sandbox/base/lifecycle/seed

install_server() {  # $1 = server 名 sbx | cli | bench；$2 = API key；$3 = 绑定的模板 code
  local kind="$1" key="$2" code="$3" sha
  kubectl -n opensandbox-system create secret generic "opensandbox-${kind}-api-key" \
    --from-literal=api-key="${key}" --dry-run=client -o yaml | kubectl apply -f -
  # ★ 模板归 Atlas 管（sandbox_template 表，经管理接口修订、发布）：ConfigMap 只在不存在时用种子建，
  #   重跑脚本不会盖掉发布过的模板
  if ! kubectl -n opensandbox-system get configmap "opensandbox-${kind}-template" >/dev/null 2>&1; then
    kubectl -n opensandbox-system create configmap "opensandbox-${kind}-template" \
      --from-file="${kind}.yaml=${SEED}/${code}.yaml"
  fi
  # ★ server 只在启动时读模板：生效内容的哈希进 Pod 注解（Atlas 发布时改的是同一个注解），
  #   内容变了才滚动重启
  sha="$(kubectl -n opensandbox-system get configmap "opensandbox-${kind}-template" \
    -o jsonpath="{.data.${kind}\.yaml}" | shasum -a 256 | cut -c1-16)"
  "$HELM" upgrade --install "opensandbox-${kind}" "$CHARTS/server" -n opensandbox-system \
    -f "deploy/local/opensandbox/${kind}-values.yaml" \
    --set-string "server.podAnnotations.atlas/template-sha=${sha}"
  kubectl -n opensandbox-system rollout status "deploy/opensandbox-${kind}" --timeout=180s
}

SBX_KEY="$(api_key sbx)"
CLI_KEY="$(api_key cli)"
# 评测档这次要装、或以前装过（它的 key 在）：Secret 里都要带上 —— 不带的话 apply 会把 key 删掉，
# 而 server 仍按 ConfigMap 去连评测档，就成了 401
BENCH_ARGS=()
if [ "${OPENSANDBOX_BENCH:-0}" = 1 ] \
  || kubectl -n opensandbox-system get secret opensandbox-bench-api-key >/dev/null 2>&1; then
  BENCH_KEY="$(api_key bench)"
  BENCH_ARGS=(--from-literal=bench_api_key="${BENCH_KEY}")
fi
# atlas-server 用同样的 key（30-server.yaml 的 OPENSANDBOX_SBX_API_KEY / OPENSANDBOX_CLI_API_KEY /
# OPENSANDBOX_BENCH_API_KEY）。★ 不开评测档时 Secret 里就没有 bench_api_key：server 那边是 optional
# ★ 空数组要写成 ${a[@]+"${a[@]}"}：macOS 自带的 bash 3.2 在 set -u 下展开空数组会报 unbound
kubectl -n atlas-system create secret generic atlas-opensandbox \
  --from-literal=sbx_api_key="${SBX_KEY}" --from-literal=cli_api_key="${CLI_KEY}" \
  ${BENCH_ARGS[@]+"${BENCH_ARGS[@]}"} --dry-run=client -o yaml | kubectl apply -f -
# 会话 Pod 的模型 key：会话 Pod 模板把它以文件挂进 Pod（/run/secrets/atlas/model_api_key）
MODEL_KEY="${ATLAS_CLI_MODEL_API_KEY:-${ATLAS_ACP_MODEL_API_KEY:-${ATLAS_CLUSTER_MODEL_API_KEY:-}}}"
# ★ 已切到模型管理平台（ConfigMap atlas-model-platform 在）：Pod 拿 LiteLLM 地址和智能体的客户端 Key，
#   不再放供应商 Key（agent-platform/doc/design/model-design.html、deploy/server/model-cutover.sh）
if kubectl -n atlas-system get configmap atlas-model-platform >/dev/null 2>&1; then
  MODEL_KEY=""
  echo "· 已经经模型管理平台：会话 Pod 里不放供应商 Key"
elif [ -z "$MODEL_KEY" ]; then
  echo "⚠ 没有会话 Pod 的模型 key：CLI 智能体将无法调用模型"
fi
kubectl -n atlas-cli create secret generic atlas-cli-model \
  --from-literal=model_api_key="${MODEL_KEY}" --dry-run=client -o yaml | kubectl apply -f -

install_server sbx "$SBX_KEY" sbx-strict
install_server cli "$CLI_KEY" cli
echo "就绪：集群内 http://opensandbox-sbx.opensandbox-system、http://opensandbox-cli.opensandbox-system"

# ── 4. 评测档（可选）────────────────────────────────────────────────
# ★ server 从 ConfigMap atlas-sandbox-bench 读地址与镜像前缀（30-server.yaml 里是 optional）。
#   改了要重启 atlas-server（env 只在启动时读）。不带 OPENSANDBOX_BENCH=1 重跑本脚本不会关掉它；
#   要关：kubectl -n atlas-system delete configmap atlas-sandbox-bench
#         helm uninstall opensandbox-bench -n opensandbox-system，再重启 atlas-server
if [ "${OPENSANDBOX_BENCH:-0}" = 1 ]; then
  install_server bench "$BENCH_KEY" bench
  kubectl -n atlas-system create configmap atlas-sandbox-bench \
    --from-literal=url="http://opensandbox-bench.opensandbox-system.svc.cluster.local" \
    --from-literal=image_prefixes="${OPENSANDBOX_BENCH_IMAGE_PREFIXES:-[\"alexgshaw/\"]}" \
    --dry-run=client -o yaml | kubectl apply -f -
  echo "评测档就绪：http://opensandbox-bench.opensandbox-system（沙箱建在 atlas-sbx，标签 atlas.profile=bench）"
fi
