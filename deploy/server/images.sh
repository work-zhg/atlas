#!/usr/bin/env bash
# 构建自建镜像的 amd64 版本，导入服务器节点的 containerd（k8s.io 命名空间）。
#
#   deploy/server/images.sh                 全部构建并导入
#   deploy/server/images.sh app sandbox     只做这几个（app frontends bridge sandbox csi）
#   deploy/server/images.sh third           第三方镜像：在服务器上用 crane 拉好、导入（kubelet 经国内加速源
#                                           拉很慢且串行，一个大镜像会堵住后面所有 Pod）
#   deploy/server/images.sh check           只检查：清单要用的镜像服务器上是否都有（deploy.sh 的前置检查调它）
#   deploy/server/images.sh pull <镜像…>    与 third 同样的办法拉指定镜像（如评测题目的镜像：
#                                           images.sh pull $(cat var/bench/agent-tasks/tbench/images.txt)）
#   SKIP_BUILD=1 deploy/server/images.sh    只导入（已构建）
#
# ★ app、frontends 每次构建打一个新标签（构建时刻，如 atlas-app:20261029-153000）：清单里的镜像变了，
#   apply 就会滚动更新，不用再手动 rollout restart；deploy.sh 默认用服务器上最新的那个标签。
#   服务器上各留最近 3 个，便于回退（IMAGE_TAG=… 可指定标签）。
# ★ 本机（Apple Silicon）用 --platform linux/amd64 交叉构建（QEMU），本机打成 *:amd64 之类的单独标签，
#   导入服务器后再改回清单期望的名字 —— 直接用原名会覆盖本机 Docker Desktop 正在用的 arm64 镜像。
# ★ 服务器没有 docker：经 ssh 把 docker save 的流灌进 ctr import，不在服务器上装构建工具。
# ★ 下载路径按实测选最快的（2026-10-20）。构建的依赖（pip / apt / npm / HuggingFace）走服务器上的代理
#   （trojan，SOCKS5，只绑服务器的 127.0.0.1:1080）比开发机代理快一个数量级：先在开发机上起
#     ssh -N -L 127.0.0.1:21080:127.0.0.1:1080 root@<服务器>
#     uvx --from pproxy pproxy -l http://0.0.0.0:28118 -r socks5://127.0.0.1:21080   # docker build 只认 HTTP 代理
#   再 BUILD_PROXY=http://host.docker.internal:28118 deploy/server/images.sh …
# ★ 脚本运行中不要改它：bash 边执行边读脚本，改动会让正在跑的那份读错位。
set -euo pipefail
cd "$(dirname "$0")/../.."

TARGET="${ATLAS_SERVER:-root@192.168.0.112}"
TAG="${IMAGE_TAG:-$(date +%Y%m%d-%H%M%S)}"
KEEP=3
PROXY="${BUILD_PROXY:-http://host.docker.internal:58591}"
PROXY_ARGS=(--build-arg "HTTP_PROXY=$PROXY" --build-arg "HTTPS_PROXY=$PROXY"
            --build-arg "http_proxy=$PROXY" --build-arg "https_proxy=$PROXY")

# 名字 → 本机 amd64 标签 | 服务器上的名字 | Dockerfile | 构建上下文
spec() {
  case "$1" in
    app)       echo "atlas-app:amd64|atlas-app:${TAG}|docker/app/Dockerfile|." ;;
    frontends) echo "atlas-frontends:amd64|atlas-frontends:${TAG}|docker/frontends/Dockerfile|." ;;
    bridge)    echo "atlas-acp-bridge:0.4.0-amd64|atlas-acp-bridge:0.4.0|docker/bridge/Dockerfile|." ;;
    sandbox)   echo "atlas-sandbox:dev-amd64|atlas-sandbox:dev|docker/sandbox/Dockerfile|docker/sandbox" ;;
    csi)       echo "atlas-csi-s3:0.43.7-amd64|atlas-csi-s3:0.43.7|docker/csi-s3/Dockerfile|docker/csi-s3" ;;
    *) echo "未知镜像 $1" >&2; exit 1 ;;
  esac
}

# 第三方镜像（与 10-infra.yaml、opensandbox values、05-csi-s3.yaml 里写的一致）
THIRD=(
  docker.io/library/postgres:16-alpine
  docker.io/library/redis:7-alpine
  docker.io/qdrant/qdrant:v1.19.1
  docker.io/jaegertracing/jaeger:2.21.0
  docker.io/otel/opentelemetry-collector:0.160.0
  docker.io/clickhouse/clickhouse-server:25.8
  docker.io/langfuse/langfuse:3
  docker.io/langfuse/langfuse-worker:3
  higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/all-in-one:2.2.4
  ghcr.io/berriai/litellm:main-stable
  registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.16.0
  sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/server:release-1.1.1
  sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/controller:release-1.1.1
  sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/execd:release-1.1.1
  sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/egress:release-1.1.1
)
# MinIO 按 digest 固定（10-infra.yaml）：digest 引用没有 tag，导入时按 digest 匹配即可
THIRD_DIGEST=(
  "$(sed -n 's/^ *image: \(cgr.dev\/chainguard\/minio@sha256:[0-9a-f]*\)$/\1/p' deploy/server/10-infra.yaml | head -1)"
  "$(sed -n 's/^ *image: \(cgr.dev\/chainguard\/minio-client@sha256:[0-9a-f]*\)$/\1/p' deploy/server/10-infra.yaml | head -1)"
)

# 标签固定的自建镜像（版本变了要改清单）：check 时与第三方镜像一起检查
FIXED=(atlas-acp-bridge:0.4.0 atlas-sandbox:dev atlas-csi-s3:0.43.7)

if [ "${1:-}" = "check" ]; then
  have="$(ssh "$TARGET" "ctr -n k8s.io images ls -q")"
  missing=()
  for img in "${THIRD[@]}" "${THIRD_DIGEST[@]}"; do
    grep -qxF "$img" <<<"$have" || missing+=("${img}（images.sh third）")
  done
  for img in "${FIXED[@]}"; do
    grep -qxF "docker.io/library/$img" <<<"$have" || missing+=("${img}（images.sh ${img%%:*}）")
  done
  for name in app frontends; do
    grep -qE "^docker.io/library/atlas-${name}:[0-9]{8}-[0-9]{6}\$" <<<"$have" \
      || missing+=("atlas-${name}:<构建时刻>（images.sh ${name}）")
  done
  if [ ${#missing[@]} -gt 0 ]; then
    printf '服务器上缺镜像：\n'; printf '  · %s\n' "${missing[@]}"; exit 1
  fi
  echo "✓ 镜像齐全"
  exit 0
fi

# 服务器上的代理（trojan 客户端，只有 SOCKS5、只绑 127.0.0.1）
SERVER_PROXY="${SERVER_PROXY:-socks5://127.0.0.1:1080}"

if [ "${1:-}" = "third" ] || [ "${1:-}" = "pull" ]; then
  if [ "$1" = "pull" ]; then
    shift
    [ $# -gt 0 ] || { echo "用法：images.sh pull <镜像…>"; exit 1; }
    IMAGES=("$@")
  else
    IMAGES=("${THIRD[@]}")
  fi
  # ★ 下载路径按实测选最快的（2026-10-20）：阿里云的仓库直连 ≈5.6 MB/s（走代理只有一半）；Docker Hub、
  #   quay、cgr.dev、registry.k8s.io 经服务器的代理 ≈0.5 MB/s，比国内加速源（daocloud 等）和开发机代理快得多。
  # ★ 用 crane 而不是 ctr pull：ctr 不读代理环境变量；给 containerd 配代理又得重启它。crane 是单个静态二进制，
  #   放在服务器的 /tmp，不装进系统。
  ssh "$TARGET" "[ -x /tmp/crane ] || (cd /tmp && curl -fsSL -x ${SERVER_PROXY/socks5:/socks5h:} -o crane.tgz \
    https://github.com/google/go-containerregistry/releases/latest/download/go-containerregistry_Linux_x86_64.tar.gz \
    && tar xzf crane.tgz crane && rm crane.tgz)"
  for img in "${IMAGES[@]}"; do
    case "$img" in
      *.aliyuncs.com/*) proxy="" ;;
      *) proxy="HTTPS_PROXY=${SERVER_PROXY}" ;;
    esac
    echo "── $img${proxy:+（经服务器代理）}"
    # 已经有了就跳过（按名字或 digest）
    if ssh "$TARGET" "ctr -n k8s.io images ls -q | grep -qxF '$img'"; then echo "   已存在"; continue; fi
    ssh "$TARGET" "set -e; f=\$(mktemp /tmp/img-XXXXXX.tar); trap 'rm -f \$f' EXIT
      $proxy /tmp/crane pull --platform linux/amd64 '$img' \$f
      ctr -n k8s.io images import --platform linux/amd64 \$f >/dev/null"
  done
  # ★ 按 digest 固定的（MinIO）由 containerd 直连 cgr.dev 拉：crane 只拉 amd64 那份 manifest，存下来的 digest
  #   与清单里的 index digest 对不上，kubelet 认不出来。cgr.dev 在服务器上直连可达，镜像也小
  if [ "${1:-}" = "third" ]; then
    for img in "${THIRD_DIGEST[@]}"; do
      echo "── ${img}（containerd 直连）"
      ssh "$TARGET" "crictl pull '$img' >/dev/null"
    done
  fi
  ssh "$TARGET" "crictl images | wc -l"
  exit 0
fi

NAMES=("$@")
[ ${#NAMES[@]} -gt 0 ] || NAMES=(app frontends bridge sandbox csi)

for name in "${NAMES[@]}"; do
  IFS='|' read -r local_tag remote_tag dockerfile context <<<"$(spec "$name")"
  if [ -z "${SKIP_BUILD:-}" ]; then
    # 第三方依赖按 uv.lock 固定（docker/app/Dockerfile 先装它，再拷源码）
    [ "$name" != app ] || docker/app/requirements.sh
    echo "── 构建 $local_tag"
    docker build --platform linux/amd64 -f "$dockerfile" -t "$local_tag" "${PROXY_ARGS[@]}" "$context"
  fi
  echo "── 导入 $remote_tag → $TARGET"
  docker save "$local_tag" | ssh "$TARGET" "ctr -n k8s.io images import --platform linux/amd64 -"
  ssh "$TARGET" "ctr -n k8s.io images tag --force docker.io/library/${local_tag} docker.io/library/${remote_tag}"
  if [ "${remote_tag#*:}" = "$TAG" ]; then
    # 按构建时刻打标签的：只留最近 KEEP 个（正在跑的 Pod 不受影响，containerd 会留着它用的层）
    ssh "$TARGET" "ctr -n k8s.io images ls -q | grep -E '^docker.io/library/${remote_tag%%:*}:[0-9]{8}-[0-9]{6}\$' \
      | sort | head -n -${KEEP} | xargs -r ctr -n k8s.io images rm >/dev/null"
  fi
done
ssh "$TARGET" "crictl images | grep -E 'atlas-' || true"
