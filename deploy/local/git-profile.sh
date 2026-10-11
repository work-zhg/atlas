#!/usr/bin/env bash
# 建 git 凭据档案 teamflow-gitee 的 Secret：TeamFlow 节点会话的 Pod 里，Agent 用它 git push 产物。
#
# 档案登记在 20-cluster.yaml 的 ATLAS_CLUSTER_GIT_PROFILES；atlas-cluster 只把它挂进
# allowed_users（TeamFlow 服务身份）的会话 Pod：/run/secrets/git（只读）。
#
# .env：
#   ATLAS_GIT_TEAMFLOW_TOKEN     写入账号的 Gitee 私人令牌（只需能写产物仓库）
#                                没配时退回 ATLAS_TF_GITEE_TOKEN（开发环境：管理与写入同一账号，权限偏大）
#   ATLAS_GIT_TEAMFLOW_USERNAME  可选；空 = 用令牌向 Gitee 查询
#   ATLAS_GIT_TEAMFLOW_EMAIL     可选；提交作者邮箱
#
# ★ 令牌只进 K8s Secret，不进任何 YAML / 日志。
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

# ★ credential helper 每次从挂载的文件读，令牌不写进 gitconfig 本身；
#   GIT_CONFIG_GLOBAL 由 atlas-cluster 指向这个文件。
GITCONFIG="$(cat <<EOF
[user]
	name = TeamFlow Agent
	email = ${EMAIL}
[credential "https://gitee.com"]
	helper = "!f() { test \$1 = get || return 0; echo username=\$(cat /run/secrets/git/username); echo password=\$(cat /run/secrets/git/token); }; f"
[init]
	defaultBranch = master
[pull]
	rebase = true
EOF
)"

kubectl -n atlas-sessions create secret generic teamflow-gitee-git \
  --from-literal=gitconfig="${GITCONFIG}" \
  --from-literal=username="${USERNAME}" \
  --from-literal=token="${TOKEN}" \
  --dry-run=client -o yaml | kubectl apply -f -
