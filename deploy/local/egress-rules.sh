#!/usr/bin/env bash
# 生成原生沙箱 egress 镜像的 deny.always：当前 kubectl 上下文所指集群的 Pod 网段和 Service 网段，
# 加链路本地网段（云上的元数据服务）。写到 docker/egress/deny.always，再构建镜像：
#
#   deploy/local/egress-rules.sh && docker build -t atlas-egress:release-1.1.1 docker/egress   # 本机
#   deploy/local/egress-rules.sh && deploy/server/images.sh egress                            # 服务器
#
#   SANDBOX_DENY_CIDRS="10.42.0.0/16 10.43.0.0/16" deploy/local/egress-rules.sh   # 手动指定，不探测
#
# ★ 默认放行时域名拒绝只在 DNS 层生效，直连 IP 绕得过去，所以这里只写 CIDR
#   （c4-model/agent/sandbox/code-sandbox-base-network.html）。opensandbox-sbx 要配 mode = "dns+nft"。
# ★ 探测不到就报错退出，不生成一份空的 —— 空文件等于没有底线。
set -euo pipefail
cd "$(dirname "$0")/../.."

out=docker/egress/deny.always
cidrs="${SANDBOX_DENY_CIDRS:-}"
if [ -z "$cidrs" ]; then
  # Pod 网段：有 Calico 就读它的 IP 池 —— Calico 自己分配 Pod IP，不按节点的 podCIDR
  #   （112 上 podCIDR 是 10.244.0.0/24，Pod 实际在 10.244.201.x，池是 10.244.0.0/16）；
  #   没有就用各节点分到的 podCIDR（单节点集群就是整个 Pod 网段）
  pods="$(kubectl get ippools.crd.projectcalico.org -o jsonpath='{range .items[*]}{.spec.cidr}{"\n"}{end}' \
    2>/dev/null || true)"
  [ -n "$pods" ] || pods="$(kubectl get nodes -o jsonpath='{range .items[*]}{.spec.podCIDRs[*]}{"\n"}{end}' \
    | tr ' ' '\n')"
  # Service 网段：有 ServiceCIDR 资源（K8s 1.31+）就读它；否则用一个不合法的 clusterIP 让 apiserver
  # 在报错里说出合法范围（dry-run，不建任何东西）
  svcs="$(kubectl get servicecidrs -o jsonpath='{range .items[*]}{.spec.cidrs[*]}{"\n"}{end}' 2>/dev/null \
    | tr ' ' '\n' || true)"
  if [ -z "$svcs" ]; then
    svcs="$(kubectl create service clusterip atlas-cidr-probe --tcp=80:80 --clusterip=1.1.1.1 \
      --dry-run=server -o name 2>&1 | sed -n 's/.*valid IPs is \([0-9a-f.:/,]*\).*/\1/p' | tr ',' '\n' || true)"
  fi
  [ -n "$pods" ] || { echo "取不到 Pod 网段，用 SANDBOX_DENY_CIDRS 指定" >&2; exit 1; }
  [ -n "$svcs" ] || { echo "取不到 Service 网段，用 SANDBOX_DENY_CIDRS 指定" >&2; exit 1; }
  cidrs="$(printf '%s\n%s\n' "$pods" "$svcs" | sed '/^$/d' | sort -u)"
fi

{
  echo "# 由 deploy/local/egress-rules.sh 生成：集群 $(kubectl config current-context 2>/dev/null || echo '?')，$(date +%F)"
  echo "# 集群的 Pod / Service 网段"
  printf '%s\n' $cidrs
  echo "# 链路本地（云上的元数据服务 169.254.169.254）"
  echo "169.254.0.0/16"
} >"$out"
echo "已写 ${out}：" >&2
cat "$out" >&2
