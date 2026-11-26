#!/usr/bin/env bash
# API 网关（c4-model/apigateway/design-api-gateway.html）：宿主机上由 systemd 管的 OpenResty，占 80 / 443。
# 在服务器上以 root 运行（仓库 git clone 在服务器上，和 cluster-setup.sh 一样）。
#
#   deploy/server/gateway.sh install   第一次：装 OpenResty（官方 apt 源）、certbot，建目录，
#                                      改写 systemd（用渲染出的配置、Restart=always），装 certbot 续期钩子、logrotate
#   deploy/server/gateway.sh           渲染 routes.yaml → /etc/openresty/atlas → openresty -t → reload（没在跑就启动）
#   deploy/server/gateway.sh cert      给还没证书的站点申请证书：先只起 80（ACME 验证），certbot --webroot，再全量渲染
#   deploy/server/gateway.sh status    公钥环是否就绪、登录失败封禁表
#   deploy/server/gateway.sh unban IP  提前解封一个 IP
#
# 参数（仓库根 .env，或环境变量）：
#   ATLAS_PUBLIC_HOST         Atlas 站点的域名（必填）
#   GATEWAY_INTERNAL_INGRESS  集群里内部 Ingress 的固定 ClusterIP:80（默认取 deploy/server/traefik-values.yaml 的 clusterIP）
#   ATLAS_LANGFUSE_HOST、ATLAS_GITEA_HOST   透传站点的域名（可空：不渲染）
#   GATEWAY_BAN_ALLOW         登录失败不计不封的网段，逗号分隔（公司出口 IP、监控探针）
#   CERTBOT_EMAIL             申请证书用的邮箱（可空：不登记邮箱，收不到到期提醒；续期由 certbot.timer 自动做）
#   CERTBOT_STAGING=1         先用 Let's Encrypt 的 staging 演练（正式环境同一域名每周最多签 5 张）
#   OPENRESTY_CODENAME        OpenResty 官方源的发行版代号；默认本机的，官方源没有时退到 bookworm / noble
#
# ★ 从 Traefik 占 80/443 换成网关是一次性的切换：deploy/server/apigateway-cutover.sh（部署新版、Traefik 退回集群内、
#   install、cert、本脚本）。之后改了 routes.yaml 只跑本脚本；deploy.sh 在服务器上跑时也会顺带跑一次。
# ★ 配置有错不会切：先换目录再 openresty -t，失败就换回旧目录，线上继续用旧配置。
set -euo pipefail
cd "$(dirname "$0")/../.."

MODE="${1:-render}"
if [ -f .env ]; then set -a; . ./.env; set +a; fi
export GATEWAY_INTERNAL_INGRESS="${GATEWAY_INTERNAL_INGRESS:-$(awk '$1 == "clusterIP:" {print $2}' deploy/server/traefik-values.yaml):80}"

CONF=/etc/openresty/atlas
STATE=/var/lib/atlas-gateway
LOGS=/var/log/atlas-gateway
ACME=/var/www/acme
NGINX=/usr/local/openresty/nginx/sbin/nginx
ADMIN="http://127.0.0.1:8081"

need_root() { [ "$(id -u)" = 0 ] || { echo "要以 root 运行"; exit 1; }; }

render_to() {  # render_to <输出目录> [--allow-missing-certs]
  local out="$1"; shift
  rm -rf "$out"
  uv run --package atlas-gateway python -m atlas_gateway --out "$out" --install-dir "$CONF" "$@"
}

# 换目录 → 检查 → reload；检查不过就换回去
apply() {
  local new="${CONF}.new" prev="${CONF}.prev"
  render_to "$new" "$@"
  rm -rf "$prev"
  [ -d "$CONF" ] && mv "$CONF" "$prev"
  mv "$new" "$CONF"
  if ! "$NGINX" -t -q -c "$CONF/nginx.conf"; then
    echo "openresty -t 不通过，换回旧配置" >&2
    rm -rf "$CONF"
    [ -d "$prev" ] && mv "$prev" "$CONF"
    exit 1
  fi
  if systemctl is-active --quiet openresty; then
    systemctl reload openresty
    echo "已 reload：旧 worker 做完手上的请求再退出（长连接最多 30 秒）"
  else
    systemctl enable --now openresty || {
      echo "启动失败：80 / 443 是不是还被 Traefik 占着？journalctl -u openresty 看原因" >&2
      exit 1
    }
    echo "已启动"
  fi
}

install() {
  need_root
  export DEBIAN_FRONTEND=noninteractive
  . /etc/os-release
  apt-get update -q
  apt-get install -y -q curl gnupg ca-certificates certbot logrotate
  local distro="$ID" codename="${OPENRESTY_CODENAME:-$VERSION_CODENAME}" base="https://openresty.org/package"
  # 官方源只到 Debian bookworm、Ubuntu noble：新发行版退一档（OpenResty 自带 OpenSSL、PCRE，依赖很少）
  if ! curl -fsI "$base/$distro/dists/$codename/Release" >/dev/null; then
    local fallback; fallback=$([ "$distro" = ubuntu ] && echo noble || echo bookworm)
    echo "OpenResty 官方源没有 $distro $codename，改用 $fallback（装好后看 openresty -V 是否正常）" >&2
    codename="$fallback"
  fi
  curl -fsSL "$base/pubkey.gpg" | gpg --dearmor --yes -o /usr/share/keyrings/openresty.gpg
  echo "deb [signed-by=/usr/share/keyrings/openresty.gpg] $base/$distro $codename openresty" \
    >/etc/apt/sources.list.d/openresty.list
  apt-get update -q
  apt-get install -y -q openresty
  systemctl stop openresty 2>/dev/null || true   # 自带的默认配置会抢 80 端口；渲染后再起

  mkdir -p "$STATE" "$LOGS" "$ACME"
  chown nobody:nogroup "$STATE"   # worker 是 nobody：要能写公钥缓存文件

  mkdir -p /etc/systemd/system/openresty.service.d
  cat >/etc/systemd/system/openresty.service.d/atlas.conf <<EOF
# 由 deploy/server/gateway.sh 写入：用渲染出的配置；进程意外退出会被拉起来
[Service]
ExecStartPre=
ExecStartPre=$NGINX -t -q -c $CONF/nginx.conf -g 'daemon on; master_process on;'
ExecStart=
ExecStart=$NGINX -c $CONF/nginx.conf -g 'daemon on; master_process on;'
ExecReload=
ExecReload=$NGINX -c $CONF/nginx.conf -g 'daemon on; master_process on;' -s reload
Restart=always
RestartSec=2
LimitNOFILE=65536
EOF
  systemctl daemon-reload

  # 证书续期后先检查再 reload；新连接用新证书，旧连接不受影响
  mkdir -p /etc/letsencrypt/renewal-hooks/deploy
  cat >/etc/letsencrypt/renewal-hooks/deploy/atlas-gateway <<EOF
#!/bin/sh
$NGINX -t -q -c $CONF/nginx.conf && systemctl reload openresty
EOF
  chmod +x /etc/letsencrypt/renewal-hooks/deploy/atlas-gateway
  systemctl enable --now certbot.timer 2>/dev/null || true

  cat >/etc/logrotate.d/atlas-gateway <<EOF
$LOGS/*.log {
  daily
  rotate 14
  missingok
  notifempty
  compress
  delaycompress
  sharedscripts
  postrotate
    [ -f /usr/local/openresty/nginx/logs/nginx.pid ] && kill -USR1 \$(cat /usr/local/openresty/nginx/logs/nginx.pid)
  endscript
}
EOF
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
  status)
    curl -s "$ADMIN/readyz"; echo
    curl -s "$ADMIN/bans"; echo
    ;;
  unban)
    : "${2:?用法：deploy/server/gateway.sh unban <IP>}"
    curl -s -X DELETE "$ADMIN/bans/$2"; echo
    ;;
  *) echo "用法：deploy/server/gateway.sh [install|render|cert|status|unban IP]"; exit 1 ;;
esac
