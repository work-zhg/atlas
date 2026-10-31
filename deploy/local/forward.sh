# 评测脚本共用（source 进来）：把集群内的 atlas-server、知识库转发到宿主（内网身份传递 §12）。
#
#   atlas-server   127.0.0.1:18000 → svc/atlas-server:8000
#   知识库         127.0.0.1:18050 → svc/atlas-knowledge:8050
#
# ★ 内网接口不对宿主暴露（Service 是 ClusterIP，网关挡掉了内网路径）；port-forward 只有持
#   kubectl 的人能用，本来就等于集群管理员，不算新增的绕过路径。脚本退出时自动关掉。
# ★ kubectl port-forward 遇到连接中断就退出（几小时的评测会被打断）：子 shell 里断了就重新拉起
_forward_pids=()

_forward() {
  local svc="$1" local_port="$2" remote_port="$3"
  (
    trap 'kill "$child" 2>/dev/null; exit 0' TERM
    while true; do
      kubectl -n atlas-system port-forward "svc/$svc" "$local_port:$remote_port" >/dev/null 2>&1 &
      child=$!
      wait "$child"
      sleep 1
    done
  ) &
  _forward_pids+=("$!")
  for _ in $(seq 1 50); do
    if (echo >"/dev/tcp/127.0.0.1/$local_port") 2>/dev/null; then return 0; fi
    sleep 0.2
  done
  echo "port-forward svc/$svc 没起来" >&2
  return 1
}

_forward_stop() {
  for pid in "${_forward_pids[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done
}
trap _forward_stop EXIT

_forward atlas-server 18000 8000
_forward atlas-knowledge 18050 8050
