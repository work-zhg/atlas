#!/usr/bin/env bash
# 把三个前端（Atlas、用户中心、AI TeamFlow）部署到本机 K8s（Docker Desktop）：一个 Pod，nginx 路由。
#
#   deploy/local/frontends.sh            构建镜像并部署
#   deploy/local/frontends.sh --no-build 只部署（镜像已构建）
#
# 前置：atlas-server / atlas-skill / 用户中心 / TeamFlow 后端已由 apply.sh 部署到集群。
# ★ 不改动、不重启 atlas-server 等已有部署。
set -euo pipefail
cd "$(dirname "$0")/../.."

if [ "${1:-}" != "--no-build" ]; then
  # 构建期从 npm 官方源装依赖；需要换源时 NPM_REGISTRY=… deploy/local/frontends.sh
  docker build -f docker/frontends/Dockerfile -t atlas-frontends:dev \
    --build-arg NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmjs.org}" .
fi

HOST_IP="${ATLAS_HOST_IP:-$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1)}"
[ -n "$HOST_IP" ] || { echo "取不到宿主 LAN IP，手动给 ATLAS_HOST_IP="; exit 1; }
echo "宿主地址 = $HOST_IP"

# 清单里写的是 atlas-frontends:dev；服务器部署经 ATLAS_FRONTENDS_IMAGE 换成本次构建的标签（见 apply.sh）
FRONTENDS_IMAGE="${ATLAS_FRONTENDS_IMAGE:-atlas-frontends:dev}"
RENDERED="$(sed -e "s/__HOST_IP__/${HOST_IP}/g" -e "s#image: atlas-frontends:dev\$#image: ${FRONTENDS_IMAGE}#" \
  deploy/local/40-frontends.yaml)"
SHA="$(printf '%s' "$RENDERED" | shasum | cut -c1-12)"
printf '%s' "$RENDERED" | sed "s/__CONFIG_SHA__/${SHA}/" | kubectl apply -f -
# 镜像标签不变（:dev）时 apply 不会触发重建：重新构建过就主动滚动一次
[ "${1:-}" = "--no-build" ] || kubectl -n atlas-system rollout restart deploy/atlas-frontends
kubectl -n atlas-system rollout status deploy/atlas-frontends --timeout=180s

echo "Atlas     → http://localhost:8080/atlas"
echo "用户中心  → http://localhost:8080/uc"
echo "TeamFlow  → http://localhost:8080/teamflow"
