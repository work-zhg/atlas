#!/usr/bin/env bash
# API 网关（c4-model/apigateway/design-api-gateway.html）：宿主机上由 systemd 管的 OpenResty，占 80 / 443。
# 在服务器上以 root 运行（仓库 git clone 在服务器上，和 cluster-setup.sh 一样）。
#
#   deploy/server/gateway.sh install   第一次：装 certbot、拉 OpenResty 镜像、建目录，写 systemd 服务 atlas-gateway
#                                      （Restart=always），装 certbot 续期钩子、logrotate
#   deploy/server/gateway.sh           渲染 routes.yaml → /etc/openresty/atlas → nginx -t → reload（没在跑就启动）
#   deploy/server/gateway.sh cert      给还没证书的站点申请证书：先只起 80（ACME 验证），certbot --webroot，再全量渲染
#   deploy/server/gateway.sh reload    不重新渲染，只检查并 reload（证书续期钩子用）
#   deploy/server/gateway.sh status    公钥环是否就绪、登录失败封禁表
#   deploy/server/gateway.sh unban IP  提前解封一个 IP
#
# ★ OpenResty 跑在官方 Docker 镜像里（host 网络，systemd 管 docker run），不用 apt 包：官方 apt 源的签名密钥
#   是 SHA1 绑定的，Debian 13 的 apt（sqv）从 2026-02-01 起拒收，退到 bookworm 源也一样。镜像固定成端到端测试
#   用的那个（infra-platform/gateway/tests/gw_harness.py），升级 = 改 GATEWAY_IMAGE、跑测试、再 install。
#   reload = 给容器里的 master 发 HUP；停止 = SIGQUIT（等长连接最多 30 秒）。
#
# 参数（仓库根 .env，或环境变量）：
#   ATLAS_PUBLIC_HOST         Atlas 站点的域名（必填）
#   GATEWAY_INTERNAL_INGRESS  集群里内部 Ingress 的固定 ClusterIP:80（默认取 deploy/server/traefik-values.yaml 的 clusterIP）
#   ATLAS_LANGFUSE_HOST、ATLAS_GITEA_HOST   透传站点的域名（可空：不渲染）
#   GATEWAY_BAN_ALLOW         登录失败不计不封的网段，逗号分隔（公司出口 IP、监控探针）
#   GATEWAY_IMAGE             OpenResty 镜像（默认 openresty/openresty:1.27.1.2-bookworm-fat）
#   CERTBOT_EMAIL             申请证书用的邮箱（可空：不登记邮箱，收不到到期提醒；续期由 certbot.timer 自动做）
#   CERTBOT_STAGING=1         先用 Let's Encrypt 的 staging 演练（正式环境同一域名每周最多签 5 张）
#
# ★ 从 Traefik 占 80/443 换成网关是一次性的切换：deploy/server/apigateway-cutover.sh（部署新版、Traefik 退回集群内、
#   install、cert、本脚本）。之后改了 routes.yaml 只跑本脚本；deploy.sh 在服务器上跑时也会顺带跑一次。
# ★ 配置有错不会切：先换目录再 nginx -t，失败就换回旧目录，线上继续用旧配置。
set -euo pipefail
cd "$(dirname "$0")/../.."

MODE="${1:-render}"
if [ -f .env ]; then set -a; . ./.env; set +a; fi
export GATEWAY_INTERNAL_INGRESS="${GATEWAY_INTERNAL_INGRESS:-$(awk '$1 == "clusterIP:" {print $2}' deploy/server/traefik-values.yaml):80}"

CONF=/etc/openresty/atlas
STATE=/var/lib/atlas-gateway
LOGS=/var/log/atlas-gateway
ACME=/var/www/acme
ADMIN="http://127.0.0.1:8081"
IMAGE="${GATEWAY_IMAGE:-openresty/openresty:1.27.1.2-bookworm-fat}"
UNIT=atlas-gateway
# ★ 挂 /etc/openresty 整个目录，不挂 $CONF：apply 换目录（mv）后，只挂 $CONF 的容器还看着旧目录
MOUNTS=(-v /etc/openresty:/etc/openresty:ro -v /etc/letsencrypt:/etc/letsencrypt:ro -v "$ACME:$ACME:ro"
        -v "$STATE:$STATE" -v "$LOGS:$LOGS")

need_root() { [ "$(id -u)" = 0 ] || { echo "要以 root 运行"; exit 1; }; }

nginx_test() {  # 用一次性容器检查配置（和线上同一个镜像、同样的挂载）
  docker run --rm --network host "${MOUNTS[@]}" "$IMAGE" nginx -t -q -c "$CONF/nginx.conf"
}

render_to() {  # render_to <输出目录> [--allow-missing-certs]
  local out="$1"; shift
  rm -rf "$out"
  uv run --package atlas-gateway python -m atlas_gateway --out "$out" --install-dir "$CONF" "$@"
}

# 起来了没有：本机运维端口有回应（公钥还没拉到时 /readyz 是 503，也算起来了）
wait_up() {
  local _
  for _ in $(seq 20); do
    curl -s -o /dev/null "$ADMIN/readyz" && return 0
    sleep 1
  done
  echo "网关没起来：80 / 443 是不是还被占着？journalctl -u $UNIT -n 50 看原因" >&2
  journalctl -u "$UNIT" -n 20 --no-pager >&2 || true
  return 1
}

# 换目录 → 检查 → reload；检查不过就换回去
apply() {
  local new="${CONF}.new" prev="${CONF}.prev"
  render_to "$new" "$@"
  rm -rf "$prev"
  [ -d "$CONF" ] && mv "$CONF" "$prev"
  mv "$new" "$CONF"
  if ! nginx_test; then
    echo "nginx -t 不通过，换回旧配置" >&2
    rm -rf "$CONF"
    [ -d "$prev" ] && mv "$prev" "$CONF"
    exit 1
  fi
  if systemctl is-active --quiet "$UNIT"; then
    systemctl reload "$UNIT"
    echo "已 reload：旧 worker 做完手上的请求再退出（长连接最多 30 秒）"
  else
    systemctl enable --now "$UNIT"
    wait_up
    echo "已启动"
  fi
}

install() {
  need_root
  export DEBIAN_FRONTEND=noninteractive
  # 以前按官方 apt 源装过（Debian 13 上签名过不了）：留着会让每次 apt-get update 报错
  rm -f /etc/apt/sources.list.d/openresty.list /usr/share/keyrings/openresty.gpg
  apt-get update -q
  apt-get install -y -q curl ca-certificates certbot logrotate
  command -v docker >/dev/null || { echo "缺 docker（cluster-setup.sh 装的）"; exit 1; }
  docker pull -q "$IMAGE"

  mkdir -p "$STATE" "$LOGS" "$ACME" /etc/openresty
  chown nobody:nogroup "$STATE"   # worker 是 nobody（容器里也是 65534）：要能写公钥缓存文件

  local mounts; mounts="$(printf '%s ' "${MOUNTS[@]}")"
  cat >"/etc/systemd/system/$UNIT.service" <<UNITEOF
# 由 deploy/server/gateway.sh 写入：API 网关（OpenResty 容器，host 网络，占 80 / 443 和本机 8081）
[Unit]
Description=Atlas API gateway (OpenResty)
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
ExecStartPre=-/usr/bin/docker rm -f $UNIT
ExecStart=/usr/bin/docker run --rm --name $UNIT --network host --ulimit nofile=65536:65536 --stop-signal SIGQUIT $mounts $IMAGE nginx -c $CONF/nginx.conf -g 'daemon off;'
ExecReload=/usr/bin/docker kill -s HUP $UNIT
ExecStop=/usr/bin/docker stop -t 35 $UNIT
Restart=always
RestartSec=2
TimeoutStopSec=45

[Install]
WantedBy=multi-user.target
UNITEOF
  systemctl daemon-reload

  # 证书续期后先检查再 reload；新连接用新证书，旧连接不受影响
  mkdir -p /etc/letsencrypt/renewal-hooks/deploy
  cat >/etc/letsencrypt/renewal-hooks/deploy/atlas-gateway <<HOOKEOF
#!/bin/sh
exec $(pwd)/deploy/server/gateway.sh reload
HOOKEOF
  chmod +x /etc/letsencrypt/renewal-hooks/deploy/atlas-gateway
  systemctl enable --now certbot.timer 2>/dev/null || true

  cat >/etc/logrotate.d/atlas-gateway <<ROTEOF
$LOGS/*.log {
  daily
  rotate 14
  missingok
  notifempty
  compress
  delaycompress
  sharedscripts
  postrotate
    docker kill -s USR1 $UNIT >/dev/null 2>&1 || true
  endscript
}
ROTEOF
  echo "装好了。下一步：deploy/server/gateway.sh cert（申请证书），之后 deploy/server/gateway.sh"
}

cert() {
  need_root
  apply --allow-missing-certs   # 没证书的站点先只起 80，给 certbot 验证
  local extra=()
  [ "${CERTBOT_STAGING:-}" = 1 ] && extra+=(--staging)
  if [ -n "${CERTBOT_EMAIL:-}" ]; then extra+=(-m "$CERTBOT_EMAIL"); else extra+=(--register-unsafely-without-email); fi
  local host
  for host in $(uv run --package atlas-gateway python -m atlas_gateway --hosts); do
    if [ -f "/etc/letsencrypt/live/$host/fullchain.pem" ]; then
      echo "$host 已有证书"
      continue
    fi
    certbot certonly --webroot -w "$ACME" -d "$host" --non-interactive --agree-tos "${extra[@]}"
  done
  apply
}

case "$MODE" in
  install) install ;;
  render) need_root; apply ;;
  cert) cert ;;
  reload) need_root; nginx_test && systemctl reload "$UNIT" ;;
  status)
    curl -s "$ADMIN/readyz"; echo
    curl -s "$ADMIN/bans"; echo
    ;;
  unban)
    : "${2:?用法：deploy/server/gateway.sh unban <IP>}"
    curl -s -X DELETE "$ADMIN/bans/$2"; echo
    ;;
  *) echo "用法：deploy/server/gateway.sh [install|render|cert|reload|status|unban IP]"; exit 1 ;;
esac
