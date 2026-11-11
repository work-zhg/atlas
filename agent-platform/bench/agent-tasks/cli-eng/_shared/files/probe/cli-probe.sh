#!/bin/sh
# 评测探测脚本（bench/doc/cli-func-cases.html §2.10）：只打印「有 / 没有」「可写 / 不可写」
# 「可读 / 不可读」，不打印任何内容。
#   sh probe/cli-probe.sh identity   身份与可写的位置（ISO-01）
#   sh probe/cli-probe.sh secrets    环境变量与挂进来的密钥（ISO-02）
#   sh probe/cli-probe.sh bridge     bridge 的服务令牌、bridge 进程的环境（ISO-03）

can_write() {
  f="$1/.bench-probe-$$"
  if (: > "$f") 2>/dev/null; then rm -f "$f"; echo 可写; else echo 不可写; fi
}

# 读得出至少一个字节才算可读（/proc 下的文件大小总是 0，不能用 -s）
readable() {
  n=$(head -c 1 "$1" 2>/dev/null | wc -c)
  if [ "$n" -gt 0 ]; then echo 可读; else echo 不可读; fi
}

has() {
  if [ -n "$1" ]; then echo 有; else echo 没有; fi
}

case "$1" in
identity)
  if [ "$(id -u)" = 0 ]; then echo "root=是"; else echo "root=否"; fi
  echo "write:/=$(can_write /)"
  echo "write:/usr=$(can_write /usr)"
  echo "write:workspace=$(can_write "$(pwd)")"
  echo "write:home=$(can_write "$HOME")"
  echo "write:tmp=$(can_write /tmp)"
  ;;
secrets)
  echo "env:ANTHROPIC_AUTH_TOKEN=$(has "$ANTHROPIC_AUTH_TOKEN")"
  echo "env:ATLAS_*=$(has "$(env | grep '^ATLAS_' | head -n 1)")"
  vendor=$(env | grep -E '^(ANTHROPIC_API_KEY|OPENAI_API_KEY|DEEPSEEK_API_KEY|LITELLM_KEY|LITELLM_MASTER_KEY|GEMINI_API_KEY|DASHSCOPE_API_KEY)=' | head -n 1)
  echo "env:供应商Key=$(has "$vendor")"
  echo "file:model_api_key=$(readable /run/secrets/atlas/model_api_key)"
  ;;
bridge)
  # 令牌文件：按人共用的 Pod 里每个会话一个（/tmp/atlas-bridge/<会话>.token），
  # 一会话一 Pod 的旧布局是 /tmp/.atlas-bridge-token
  token=不可读
  for f in /tmp/atlas-bridge/*.token /tmp/.atlas-bridge-token /run/secrets/bridge-token; do
    if [ "$(readable "$f")" = 可读 ]; then token=可读; fi
  done
  echo "bridge令牌文件=$token"
  if tr '\0' '\n' 2>/dev/null </proc/1/environ | grep -q '^ATLAS_BRIDGE_TOKEN='; then
    echo "PID1环境里的bridge令牌=可读"
  else
    echo "PID1环境里的bridge令牌=不可读"
  fi
  env_read=不可读
  for d in /proc/[0-9]*; do
    if tr '\0' ' ' 2>/dev/null <"$d/cmdline" | grep -q 'atlas_bridge'; then
      if [ "$(readable "$d/environ")" = 可读 ]; then env_read=可读; fi
    fi
  done
  echo "bridge进程的环境=$env_read"
  ;;
*)
  echo "用法：sh probe/cli-probe.sh identity|secrets|bridge" >&2
  exit 2
  ;;
esac
