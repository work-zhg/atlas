#!/usr/bin/env bash
# 建出站凭据档案 teamflow-gitee：TeamFlow 节点会话的会话 Pod 里，Agent 用它 git push 产物。
#
# 档案是 atlas-server 的 CREDENTIAL_PROFILES（JSON：code → 注入方式 + 主机 + 凭据 + 谁能用，
# c4-model/agent/code-credentials.html），放在 Secret atlas-credential-profiles 里（30-server.yaml 引用）。
# 凭据组件只给 source_app = teamflow 的会话授权；会话 Pod 每轮把凭据整份写进 Pod 的 Credential Vault，
# 由 egress sidecar 在发往 gitee.com 的 HTTPS 请求上注入（basic）—— Pod 里没有凭据文件，也没有环境变量。
# 要加别的档案（私有 npm / PyPI 源按 bearer 或 api_key 写），在下面的 JSON 里加一项。
#
# .env：
#   ATLAS_GIT_TEAMFLOW_TOKEN     写入账号的 Gitee 私人令牌（只需能写产物仓库）
#                                没配时退回 ATLAS_TF_GITEE_TOKEN（开发环境：管理与写入同一账号，权限偏大）
#   ATLAS_GIT_TEAMFLOW_USERNAME  可选；空 = 用令牌向 Gitee 查询
#   ATLAS_GIT_TEAMFLOW_EMAIL     可选；提交作者邮箱
#
# ★ 令牌只进 K8s Secret，不进任何 YAML / 日志。改完要重启 atlas-server（env 只在启动时读）。
set -euo pipefail
cd "$(dirname "$0")/../.."

[ -f .env ] || { echo "缺 .env"; exit 1; }
# shellcheck disable=SC1091
set -a; . ./.env; set +a

TOKEN="${ATLAS_GIT_TEAMFLOW_TOKEN:-${ATLAS_TF_GITEE_TOKEN:-}}"
[ -n "$TOKEN" ] || { echo "缺 ATLAS_GIT_TEAMFLOW_TOKEN（或 ATLAS_TF_GITEE_TOKEN）"; exit 1; }
[ -n "${ATLAS_GIT_TEAMFLOW_TOKEN:-}" ] || echo "⚠ 未配 ATLAS_GIT_TEAMFLOW_TOKEN，暂用仓库管理令牌（只适合开发环境）"

USERNAME="${ATLAS_GIT_TEAMFLOW_USERNAME:-}"
if [ -z "$USERNAME" ]; then
  # gitee.com 直连；令牌放请求头，不进 URL
  USERNAME="$(curl -fsS --noproxy '*' -H "Authorization: token ${TOKEN}" https://gitee.com/api/v5/user \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["login"])')"
fi
EMAIL="${ATLAS_GIT_TEAMFLOW_EMAIL:-${USERNAME}@users.noreply.gitee.com}"
echo "写入账号 = ${USERNAME}"

# ★ JSON 用 python 拼：令牌里有特殊字符也不会破坏格式；经 stdin 传，不出现在进程参数里
PROFILES="$(TOKEN="$TOKEN" USERNAME="$USERNAME" EMAIL="$EMAIL" python3 -c '
import json, os
print(json.dumps({"teamflow-gitee": {
    "auth": "basic", "hosts": ["gitee.com"],
    "username": os.environ["USERNAME"], "secret": os.environ["TOKEN"],
    "apps": ["teamflow"],
    "author_name": "TeamFlow Agent", "author_email": os.environ["EMAIL"],
}}))')"

kubectl -n atlas-system create secret generic atlas-credential-profiles \
  --from-literal=profiles="${PROFILES}" \
  --dry-run=client -o yaml | kubectl apply -f -
