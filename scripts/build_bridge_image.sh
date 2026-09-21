#!/usr/bin/env bash
# 构建会话 Pod 的镜像，并导入本机 k3s 的 containerd。
#
# 用法：  ./scripts/build_bridge_image.sh [name:tag]
#
# ★ 为什么不是 docker build：这套环境里没有 docker 守护进程。BuildKit 的
#   独立发行包自带 runc，解压即用，不需要包管理器也不需要守护进程常驻。
#
# ★ 为什么最后要 ctr import：镜像是本地构建的，registry 上不存在 ——
#   Pod 若走 pull 一定失败（ImagePullBackOff）。导入之后节点上就有了，
#   配合 imagePullPolicy 的默认行为（本地有就不拉）即可使用。
set -euo pipefail

IMAGE="${1:-atlas-acp-bridge:0.1.0}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${TMPDIR:-/tmp}/${IMAGE//[:\/]/-}.tar"

command -v buildctl >/dev/null || {
  echo "缺 buildctl。装法（独立二进制，自带 runc）：" >&2
  echo "  VER=v0.33.0" >&2
  echo "  curl -sL https://github.com/moby/buildkit/releases/download/\$VER/buildkit-\$VER.linux-amd64.tar.gz \\" >&2
  echo "    | tar xz -C /usr/local" >&2
  exit 1
}

# buildkitd 没起就起一个（OCI worker，不依赖 containerd）
if ! buildctl debug workers >/dev/null 2>&1; then
  echo "==> 启动 buildkitd"
  buildkitd --oci-worker=true --containerd-worker=false >/tmp/buildkitd.log 2>&1 &
  for _ in $(seq 1 30); do
    buildctl debug workers >/dev/null 2>&1 && break
    sleep 1
  done
  buildctl debug workers >/dev/null 2>&1 || { echo "buildkitd 起不来，看 /tmp/buildkitd.log" >&2; exit 1; }
fi

echo "==> 构建 ${IMAGE}"
buildctl build \
  --frontend dockerfile.v0 \
  --local "context=${ROOT}" \
  --local "dockerfile=${ROOT}/docker/bridge" \
  --output "type=oci,dest=${OUT},name=${IMAGE}"

echo "==> 导入 k3s containerd"
k3s ctr images import "${OUT}"
rm -f "${OUT}"

# 确认节点上真的有了 —— 构建成功但没导进去的话，Pod 会以
# ImagePullBackOff 失败，而那条错误指向 registry，离真因很远。
#
# ★ 用 inspecti 而不是 `images -q <ref>`：后者**根本不过滤**，随便给个
#   不存在的名字它也照样把所有镜像列出来。拿它当"在不在"的判据，
#   结果是只要节点上有任意镜像就判成功。
if crictl inspecti -q "docker.io/library/${IMAGE}" >/dev/null 2>&1; then
  echo "==> 就绪：docker.io/library/${IMAGE}"
else
  echo "导入后仍查不到镜像：docker.io/library/${IMAGE}" >&2
  exit 1
fi
