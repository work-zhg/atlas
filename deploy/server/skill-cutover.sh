#!/usr/bin/env bash
# 配置服务（atlas-config）改为技能应用（atlas-skill）的一次性切换。可重复执行：做过的步骤会跳过。
#
#   deploy/server/skill-cutover.sh
#
# 前置：新的 app、frontends 镜像已导入服务器（deploy/server/images.sh app frontends）；
#   仓库根 .env 与 deploy.sh 用的是同一份（有 UC_ADMIN_PASSWORD 时第 4 步会顺带登记到用户中心）。
#
# 做五件事：
#   1. 停掉 atlas-config（删 Deployment）：之后没人再写配置库
#   2. 导出配置库里的持久指令（instruction 表）→ 本机 var/cutover/skill-instructions.sql
#   3. 库改名 atlas_config → atlas_skill：技能、版本、审计原样保留；对象存储里的技能包路径不变
#   4. deploy/server/deploy.sh app：Atlas 迁移到 0027（建 instruction 表）；atlas-skill 迁移到 c0004
#      （删掉技能库里的 instruction、审计表改名）；登记到用户中心 —— 技能应用在 Atlas 之前，Atlas 退役
#      skill:upload、skill:review 时把持有人换成技能应用的 SKILL_AUTHOR、SKILL_ADMIN
#   5. 把第 2 步导出的指令导入 Atlas 的库（同一份已经有的不覆盖）
#
# ★ 第 4 步没有登记到用户中心（.env 没有 UC_ADMIN_PASSWORD）时，之后手动跑 deploy/server/register.sh；
#   没登记之前技能页对所有人都是 403（技能应用的操作码还没登记）。
# ★ 回退：先 ALTER DATABASE atlas_skill RENAME TO atlas_config，再用旧镜像跑 deploy.sh app
#   （ATLAS_APP_IMAGE=…）；c0004 删掉的指令从第 2 步的导出文件补回。导出文件确认无误后可以删掉。
set -euo pipefail
cd "$(dirname "$0")/../.."

SERVER_IP="${ATLAS_SERVER_IP:-192.168.0.112}"
export NO_PROXY="${NO_PROXY:+${NO_PROXY},}${SERVER_IP}" no_proxy="${no_proxy:+${no_proxy},}${SERVER_IP}"
export KUBECONFIG="${ATLAS_KUBECONFIG:-$HOME/.kube/atlas-${SERVER_IP}.yaml}"
EXPORT=var/cutover/skill-instructions.sql

pg() { kubectl -n atlas-infra exec -i statefulset/postgres -- psql -U atlas -v ON_ERROR_STOP=1 "$@"; }
db_exists() { pg -d atlas -tAc "SELECT 1 FROM pg_database WHERE datname='$1'" | grep -q 1; }
has_instructions() { [ "$(pg -d "$1" -tAc "SELECT to_regclass('public.instruction') IS NOT NULL")" = t ]; }

echo "── 1. 停掉 atlas-config"
kubectl -n atlas-system delete deployment atlas-config --ignore-not-found --wait=true
kubectl -n atlas-system wait --for=delete pod -l app=atlas-config --timeout=120s 2>/dev/null || true

echo "── 2. 导出持久指令"
SOURCE=""
if db_exists atlas_config; then SOURCE=atlas_config; elif db_exists atlas_skill; then SOURCE=atlas_skill; fi
if [ -n "$SOURCE" ] && has_instructions "$SOURCE"; then
  mkdir -p "$(dirname "$EXPORT")"
  # 配置服务里 owner_id / updated_by 存的是 X-Uid 原文：个人指令的 owner 统一成小写 uuid，
  # 不是 uuid 的操作人记空（Atlas 那边是 uuid 列）。只搬有内容的
  pg -d "$SOURCE" -tAc "
    SELECT format(
      'INSERT INTO instruction (scope, owner_id, content, updated_by, updated_at) VALUES (%L, %L, %L, %s, %L) ON CONFLICT (scope, owner_id) DO NOTHING;',
      scope,
      CASE WHEN scope = 'user' THEN lower(owner_id) ELSE owner_id END,
      content,
      CASE WHEN updated_by ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\$'
           THEN quote_literal(lower(updated_by)) || '::uuid' ELSE 'NULL' END,
      updated_at)
    FROM instruction WHERE content <> '' ORDER BY scope, owner_id" > "$EXPORT"
  echo "  $(grep -c '^INSERT' "$EXPORT" || true) 条 → $EXPORT"
elif [ -s "$EXPORT" ]; then
  echo "  技能库里已经没有 instruction 表，沿用上次的导出 $EXPORT"
else
  echo "  没有要搬的指令"
fi

echo "── 3. 库改名 atlas_config → atlas_skill"
if db_exists atlas_config; then
  if db_exists atlas_skill; then
    echo "✗ atlas_config 与 atlas_skill 两个库都在：确认 atlas_skill 里没有要的数据后删掉它，再跑一次"
    exit 1
  fi
  pg -d atlas -tAc "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity
                    WHERE datname = 'atlas_config' AND pid <> pg_backend_pid()" >/dev/null
  pg -d atlas -c "ALTER DATABASE atlas_config RENAME TO atlas_skill"
else
  echo "  已经改过"
fi

echo "── 4. 部署（deploy.sh app）"
deploy/server/deploy.sh app

echo "── 5. 导入持久指令到 Atlas 的库"
if [ -s "$EXPORT" ]; then
  pg -d atlas -q < "$EXPORT"
  echo "  Atlas 里现在有 $(pg -d atlas -tAc "SELECT count(*) FROM instruction") 条指令"
  echo "  确认无误后可以删掉 $EXPORT"
else
  echo "  没有要导入的"
fi
echo "✓ 切换完成：技能页走 /skill-api；平台 / 个人指令在 Atlas（/atlas-api/v1/instructions）"
