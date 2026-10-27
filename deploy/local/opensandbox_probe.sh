#!/usr/bin/env bash
# 在集群内跑对真 OpenSandbox 的实测。
#
#   deploy/local/opensandbox_probe.sh [镜像] [--restart-server]   原生沙箱（opensandbox_probe.py）
#   deploy/local/opensandbox_probe.sh [镜像] acp                  ACP Pod（opensandbox_acp_probe.py）
#
# 镜像默认 atlas-app:dev，要包含 providers/opensandbox。
# --restart-server：脚本跑到「现在重启 opensandbox-sbx」时，在另一个终端执行
#   kubectl -n opensandbox-system rollout restart deploy/opensandbox-sbx
# acp：用 git 档案 teamflow-gitee（Secret atlas-git-profiles，git-profile.sh 建）与 CLI 镜像
#   ${PROBE_CLI_IMAGE:-atlas-acp-bridge:0.4.0}；PROBE_GIT_REPO 给一个该档案能读的私有仓库才测 git（只读）。
# ★ Redis 用测试库（db 14），数据库只是凑配置、不会连；跑完 release 掉建的沙箱。
set -euo pipefail
cd "$(dirname "$0")/../.."
IMAGE="${1:-atlas-app:dev}"; shift || true
HOST_IP="${ATLAS_HOST_IP:-$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1)}"
# shellcheck disable=SC1091
[ -f .env ] && { set -a; . ./.env; set +a; }
MODEL_URL="${ATLAS_ACP_MODEL_BASE_URL:-${ATLAS_CLUSTER_MODEL_BASE_URL:-}}"
# 服务器上（deploy/server）Redis 在集群内：PROBE_REDIS_URL=redis://redis.atlas-infra.svc.cluster.local:6379/14
PROBE_REDIS_URL="${PROBE_REDIS_URL:-redis://${HOST_IP}:6380/14}"
SCRIPT=opensandbox_probe.py
if [ "${1:-}" = "acp" ]; then SCRIPT=opensandbox_acp_probe.py; shift; fi
ARGS=$(printf '"%s",' "$@"); ARGS="[\"python\", \"/probe/${SCRIPT}\"${ARGS:+, ${ARGS%,}}]"

kubectl -n atlas-system delete pod opensandbox-probe --ignore-not-found >/dev/null
kubectl -n atlas-system create configmap opensandbox-probe \
  --from-file=opensandbox_probe.py=deploy/local/opensandbox_probe.py \
  --from-file=opensandbox_acp_probe.py=deploy/local/opensandbox_acp_probe.py \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl apply -f - <<YAML
apiVersion: v1
kind: Pod
metadata: {name: opensandbox-probe, namespace: atlas-system}
spec:
  restartPolicy: Never
  containers:
    - name: probe
      image: ${IMAGE}
      imagePullPolicy: Never
      command: ${ARGS}
      env:
        - {name: DATABASE_URL, value: "postgresql+asyncpg://atlas:atlas@${HOST_IP}:5433/atlas_server_test"}
        - {name: REDIS_URL, value: "${PROBE_REDIS_URL}"}
        - {name: LITELLM_KEY, value: unused}
        - {name: SANDBOX_ENABLED, value: "true"}
        - {name: OPENSANDBOX_SBX_URL, value: "http://opensandbox-sbx.opensandbox-system"}
        - name: OPENSANDBOX_SBX_API_KEY
          valueFrom: {secretKeyRef: {name: atlas-opensandbox, key: sbx_api_key}}
        - {name: OPENSANDBOX_ACP_URL, value: "http://opensandbox-acp.opensandbox-system"}
        - name: OPENSANDBOX_ACP_API_KEY
          valueFrom: {secretKeyRef: {name: atlas-opensandbox, key: acp_api_key}}
        - {name: ACP_MODEL_BASE_URL, value: "${MODEL_URL}"}
        - {name: ACP_MODEL_NAME, value: deepseek-chat}
        - name: GIT_PROFILES
          valueFrom: {secretKeyRef: {name: atlas-git-profiles, key: profiles, optional: true}}
        - {name: CLI_IMAGE, value: "${PROBE_CLI_IMAGE:-atlas-acp-bridge:0.4.0}"}
        - {name: ADAPTER, value: "node /opt/acp-cli/bin/claude-acp"}
        - {name: PROBE_GIT_REPO, value: "${PROBE_GIT_REPO:-}"}
      volumeMounts: [{name: probe, mountPath: /probe}]
  volumes: [{name: probe, configMap: {name: opensandbox-probe}}]
YAML
kubectl -n atlas-system wait --for=condition=Ready pod/opensandbox-probe --timeout=60s >/dev/null || true
kubectl -n atlas-system logs -f opensandbox-probe
kubectl -n atlas-system delete pod opensandbox-probe --wait=false >/dev/null
kubectl -n atlas-system delete configmap opensandbox-probe >/dev/null
