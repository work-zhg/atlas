#!/usr/bin/env bash
# 给评测档沙箱接上出网代理（26-bench-egress.yaml）：起 gost，把代理地址交给 atlas-server。
#
#   deploy/server/bench-egress.sh          装 / 更新（口令已有就沿用）
#   deploy/server/bench-egress.sh off      撤掉（atlas-server 下次启动时评测档就不带代理了）
#
# 前置：已经 OPENSANDBOX_BENCH=1 部署了评测档；镜像 gogost/gost 已导入
#   （deploy/server/images.sh pull docker.io/gogost/gost:3.3.2）。
# ★ 代理地址进 Secret atlas-sandbox-bench-proxy（30-server.yaml 里 optional），不放进 atlas-opensandbox：
#   opensandbox.sh 每次会整份重建那个 Secret。改完要重启 atlas-server（env 只在启动时读）。
set -euo pipefail
cd "$(dirname "$0")/../.."

SERVER_IP="${ATLAS_SERVER_IP:-192.168.0.112}"
export KUBECONFIG="${ATLAS_KUBECONFIG:-$HOME/.kube/atlas-${SERVER_IP}.yaml}"
# ★ 内网直连，不走开发机代理
export NO_PROXY="${NO_PROXY:+${NO_PROXY},}${SERVER_IP}" no_proxy="${no_proxy:+${no_proxy},}${SERVER_IP}"

if [ "${1:-}" = "off" ]; then
  kubectl -n atlas-system delete secret atlas-sandbox-bench-proxy --ignore-not-found
  kubectl -n atlas-infra delete -f deploy/server/26-bench-egress.yaml --ignore-not-found
  kubectl -n atlas-infra delete secret bench-egress --ignore-not-found
  kubectl -n atlas-system rollout restart deploy/atlas-server
  exit 0
fi

password="$(kubectl -n atlas-infra get secret bench-egress -o jsonpath='{.data.password}' 2>/dev/null \
  | base64 --decode || true)"
[ -n "$password" ] || password="$(openssl rand -hex 16)"
kubectl -n atlas-infra create secret generic bench-egress --from-literal=password="${password}" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f deploy/server/26-bench-egress.yaml
kubectl -n atlas-infra rollout status deploy/bench-egress --timeout=180s

# 沙箱经节点 IP 连它（hostNetwork）
kubectl -n atlas-system create secret generic atlas-sandbox-bench-proxy \
  --from-literal=url="http://bench:${password}@${SERVER_IP}:28118" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n atlas-system rollout restart deploy/atlas-server
kubectl -n atlas-system rollout status deploy/atlas-server --timeout=300s
echo "✓ 评测档沙箱出网经 ${SERVER_IP}:28118（新建的沙箱才带上；已在跑的要等回收后重建）"
