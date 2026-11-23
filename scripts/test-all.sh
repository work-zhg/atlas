#!/usr/bin/env bash
# 按目录逐个跑全部测试：pyproject.toml 的 testpaths，每个目录一个 pytest 进程。
#
# 用法：  scripts/test-all.sh [pytest 参数 …]
#
# ★ 为什么不一次跑完：各服务的测试目录都叫 tests、没有 __init__.py，conftest.py 和同名的测试文件
#   （test_api.py、test_tenancy.py、test_jobs_api.py ……）在一个进程里会互相顶掉，收集阶段就报错。
#   分进程跑，每个目录只看得到自己的 conftest。
# ★ Agent 服务的测试会清表：DATABASE_URL 要指向 …/atlas_test（conftest 会拦开发库）；
#   其余服务的测试各自连写死的 *_test 库。
# ★ 一个目录失败不停，跑完再汇总，退出码非零。
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# macOS 自带的 bash 3.2 没有 mapfile
dirs=()
while IFS= read -r dir; do
  [ -n "$dir" ] && dirs+=("$dir")
done < <(uv run python -c '
import tomllib
with open("pyproject.toml", "rb") as f:
    print("\n".join(tomllib.load(f)["tool"]["pytest"]["ini_options"]["testpaths"]))
')
[ "${#dirs[@]}" -gt 0 ] || { echo "pyproject.toml 里没有 testpaths" >&2; exit 2; }

failed=()
for dir in "${dirs[@]}"; do
  echo "::group::${dir}"
  if ! uv run pytest -q "$dir" "$@"; then
    failed+=("$dir")
  fi
  echo "::endgroup::"
done

if [ "${#failed[@]}" -gt 0 ]; then
  echo "失败的目录（${#failed[@]}/${#dirs[@]}）：" >&2
  printf '  %s\n' "${failed[@]}" >&2
  exit 1
fi
echo "全部通过（${#dirs[@]} 个目录）"
