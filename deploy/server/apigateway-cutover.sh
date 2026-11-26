#!/usr/bin/env bash
# 一次性：对外入口从「Traefik 占 80/443 → 前端 Pod 里的 nginx」换成宿主机上的 API 网关
# （c4-model/apigateway/design-api-gateway.html）。在服务器的仓库里以 root 运行；可重复执行。
#
#   deploy/server/apigateway-cutover.sh
#
# 前置：
#   · 新镜像已导入（deploy/server/images.sh）：前端是 /<名>-ui 的新路径，用户中心签访问 JWT
#   · .env：ATLAS_PUBLIC_HOST（已解析到本机，80 对公网开放：证书用 HTTP-01 验证）；CERTBOT_EMAIL 可空；
#     要从外面开 Langfuse 就再写 ATLAS_LANGFUSE_HOST（同样要解析到本机）
#
# 步骤（站点从第 2 步断开，第 4 步结束恢复；单节点上几分钟）：
#   1. 装 OpenResty、certbot（gateway.sh install；不占端口，Traefik 照常对外）
#   2. 部署新版（deploy.sh app）：各服务的内部 Ingress、去掉 nginx 的前端、用户中心的 JWT、系统租户
#   3. Traefik 退回集群内：不占 80/443、固定 ClusterIP（traefik-values.yaml），从宿主机试一次内部 Ingress
#   4. 申请证书并起网关（gateway.sh cert：先只起 80 做 ACME 验证，再全量）
#   5. 检查：公钥环就绪、页面能开、接口未登录回 401
#
# ★ Traefik 以前的 Let's Encrypt 证书在它的 PVC 里，网关不用：certbot 重新申请（同一域名每周最多 5 张，
#   先演练就 CERTBOT_STAGING=1，正式前把 /etc/letsencrypt/live/<域名> 删掉再跑 gateway.sh cert）。
# ★ 回退：helm upgrade traefik 用切换前的值（hostPort 80/443）、systemctl stop openresty；前端镜像要换回旧标签。
set -euo pipefail
cd "$(dirname "$0")/../.."

[ "$(id -u)" = 0 ] || { echo "要以 root 运行"; exit 1; }
[ -r /etc/kubernetes/admin.conf ] || { echo "要在服务器（K8s 节点）上运行"; exit 1; }
export KUBECONFIG=/etc/kubernetes/admin.conf
[ -f .env ] || { echo "缺 .env"; exit 1; }
# shellcheck disable=SC1091
set -a; . ./.env; set +a
: "${ATLAS_PUBLIC_HOST:?.env 要写 ATLAS_PUBLIC_HOST}"
command -v helm >/dev/null || { echo "缺 helm（cluster-setup.sh 装的）"; exit 1; }
HOST="$ATLAS_PUBLIC_HOST"
INGRESS_IP="$(awk '$1 == "clusterIP:" {print $2}' deploy/server/traefik-values.yaml)"

echo "── 1. 装 OpenResty、certbot"
[ -x /usr/local/openresty/nginx/sbin/nginx ] || deploy/server/gateway.sh install

echo "── 2. 部署新版"
ATLAS_GATEWAY_CUTOVER=1 ATLAS_PUBLIC_SCHEME=https deploy/server/deploy.sh app

echo "── 3. Traefik 退回集群内（ClusterIP ${INGRESS_IP}）"
old_ip="$(kubectl -n traefik get svc traefik -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)"
[ -z "$old_ip" ] || [ "$old_ip" = "$INGRESS_IP" ] || kubectl -n traefik delete svc traefik
helm repo update traefik >/dev/null
helm upgrade --install traefik traefik/traefik --version 42.0.0 -n traefik --create-namespace \
  -f deploy/server/traefik-values.yaml
kubectl -n traefik rollout status deploy/traefik --timeout=180s
# hostPort 是 DNAT 规则，不是监听：等占着它的旧 Pod 完全删掉，宿主机的 80 / 443 才真正让出来
for _ in $(seq 60); do
  [ -z "$(kubectl -n traefik get pods -o jsonpath='{.items[*].spec.containers[*].ports[*].hostPort}')" ] && break
  sleep 2
done
code="$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: uc.internal.techstz.com' \
  "http://${INGRESS_IP}/api/v1/auth/jwks" || true)"
[ "$code" = 200 ] || { echo "✗ 宿主机经内部 Ingress 取不到用户中心的公钥集（${code}）：kubectl get ingress -A 核对"; exit 1; }
echo "内部 Ingress 通了"

echo "── 4. 证书与网关"
deploy/server/gateway.sh cert

echo "── 5. 检查"
for _ in $(seq 15); do curl -fs http://127.0.0.1:8081/readyz >/dev/null && break; sleep 2; done
deploy/server/gateway.sh status
check() {  # check <路径> <期望状态码>
  local got
  got="$(curl -s -o /dev/null -w '%{http_code}' --resolve "${HOST}:443:127.0.0.1" "https://${HOST}$1" || true)"
  printf '  %-28s %s（期望 %s）\n' "$1" "$got" "$2"
  [ "$got" = "$2" ]
}
ok=1
check /atlas-ui/ 200 || ok=0
check /uc-ui/login 200 || ok=0
check /atlas/v1/threads 401 || ok=0
check /uc/api/v1/auth/jwks 200 || ok=0
[ "$ok" = 1 ] || { echo "✗ 有地址不对：journalctl -u openresty、/var/log/atlas-gateway/ 看原因"; exit 1; }
echo "✓ 切换完成：https://${HOST}/（跳 /atlas-ui/）；统一登录 https://${HOST}/uc-ui/login"
echo "  运营中心 https://${HOST}/opscenter-ui/：系统租户的 admin 登录（临时密码见第 2 步的输出），在用户中心给运营人员授运营模块"
