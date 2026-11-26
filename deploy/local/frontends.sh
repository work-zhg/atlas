#!/usr/bin/env bash
# 把前端（Atlas、用户中心、AI TeamFlow 一个 Pod；运营中心自己一个）部署到 K8s。
#
#   deploy/local/frontends.sh            构建镜像并部署
#   deploy/local/frontends.sh --no-build 只部署（镜像已构建）
#
# 对外经 API 网关（宿主机上的 OpenResty，infra-platform/gateway）：页面 /<名>-ui/，接口 /<名>/；
# 清单里的 Ingress 是集群内的（Host = <名>-ui.internal.techstz.com）。
# ★ 不改动、不重启 atlas-server 等已有部署。
set -euo pipefail
cd "$(dirname "$0")/../.."

if [ "${1:-}" != "--no-build" ]; then
  # 构建期从 npm 官方源装依赖；需要换源时 NPM_REGISTRY=… deploy/local/frontends.sh
  docker build -f docker/frontends/Dockerfile -t atlas-frontends:dev \
    --build-arg NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmjs.org}" .
fi

# 清单里写的是 atlas-frontends:dev；服务器部署经 ATLAS_FRONTENDS_IMAGE 换成本次构建的标签（见 apply.sh）
FRONTENDS_IMAGE="${ATLAS_FRONTENDS_IMAGE:-atlas-frontends:dev}"
# 一次性：以前前端 Pod 里的 nginx 是对外入口（LoadBalancer :8080），现在只在集群内。
# 直接 apply 改不了类型（已分配的 nodePort 会被拒），先删旧的再建
if [ "$(kubectl -n atlas-system get svc atlas-frontends -o jsonpath='{.spec.type}' 2>/dev/null)" = "LoadBalancer" ]; then
  kubectl -n atlas-system delete svc atlas-frontends
fi
for f in deploy/local/40-frontends.yaml deploy/local/49-opscenter-web.yaml; do
  sed -e "s#image: atlas-frontends:dev\$#image: ${FRONTENDS_IMAGE}#" "$f" | kubectl apply -f -
done
# 镜像标签不变（:dev）时 apply 不会触发重建：重新构建过就主动滚动一次
[ "${1:-}" = "--no-build" ] || kubectl -n atlas-system rollout restart deploy/atlas-frontends deploy/atlas-opscenter-web
kubectl -n atlas-system rollout status deploy/atlas-frontends --timeout=180s
kubectl -n atlas-system rollout status deploy/atlas-opscenter-web --timeout=180s

echo "页面经 API 网关：/atlas-ui/、/uc-ui/、/teamflow-ui/、/opscenter-ui/"
echo "本机没有网关：kubectl -n atlas-system port-forward svc/atlas-frontends 3000 只能看页面（接口要经网关）"
