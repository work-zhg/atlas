#!/bin/sh
# 评测计数脚本（bench/doc/sandbox-eng-cases.html §0「副作用计数」）
#   sh marks/mark.sh <标记> [秒数]
# 开始时建 marks/<标记>/start-<id>（记下沙箱的 hostname，即 Pod 名），每秒打印一行 tick <n>，
# 结束时建 end-<id>（带随机令牌），最后一行打印 MARK-DONE <标记> <令牌>。
# 每次执行建新文件而不是追加：/workspace 是 s3fs 挂载，并发追加会互相覆盖。
label="$1"
secs="${2:-0}"
[ -n "$label" ] || { echo "用法：sh marks/mark.sh <标记> [秒数]" >&2; exit 2; }
dir="$(dirname "$0")/$label"
mkdir -p "$dir"
id="$$-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
printf 'host=%s pid=%s at=%s\n' "$(cat /proc/sys/kernel/hostname)" "$$" "$(date +%s)" > "$dir/start-$id"
i=0
while [ "$i" -lt "$secs" ]; do
  sleep 1
  i=$((i + 1))
  echo "tick $i"
done
token="T$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
printf 'token=%s at=%s\n' "$token" "$(date +%s)" > "$dir/end-$id"
echo "MARK-DONE $label $token"
