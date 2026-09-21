"""kernel/ 的护栏。

kernel 是本项目自有代码，
这里守住两件事：
  1. 内部导入一律用 atlas_engine.kernel.* —— 漏一处会在运行时才炸
  2. 环境里没有同名的第三方包 —— 否则改了 kernel 却跑着别人的代码

裁剪推进时，本文件的断言应随之更新。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

KERNEL = Path(__file__).resolve().parents[1] / "engine/src/atlas_engine/kernel"


def test_kernel_imports() -> None:
    from atlas_engine import kernel

    assert getattr(kernel, "__all__", None)


def test_entry_points_used_by_atlas_import() -> None:
    """Atlas 实际用到的入口。裁剪时若动了这几处，这里会先失败。"""
    from atlas_engine.kernel.graph import create_deep_agent
    from atlas_engine.kernel.middleware.filesystem import FilesystemMiddleware
    from atlas_engine.kernel.middleware.subagents import SubAgentMiddleware

    assert all(
        callable(x) or isinstance(x, type)
        for x in (create_deep_agent, FilesystemMiddleware, SubAgentMiddleware)
    )


def test_namespace_rewrite_is_complete() -> None:
    """kernel 内部必须一律用 `atlas_engine.kernel.*` 绝对导入。

    漏一处不会在 import kernel 时暴露，而是等到那条代码路径被走到才炸，
    报错还是"模块不存在"—— 离真正的病根很远，所以用测试钉住。
    """
    pattern = re.compile(r"^\s*(from|import)\s+deepagents\b")
    offenders = [
        f"{path.relative_to(KERNEL)}:{i}"
        for path in KERNEL.rglob("*.py")
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert not offenders, f"仍有未改写的绝对导入: {offenders[:10]}"


def test_no_third_party_deepagents_installed() -> None:
    """环境里不该存在会与 kernel 同名冲突的第三方包。

    否则会出现"改了 kernel 却跑着 site-packages 那份"的错觉，极难排查。
    """
    result = subprocess.run(
        [sys.executable, "-c", "import deepagents"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, "环境里存在同名第三方包，会与 kernel 混淆"



@pytest.mark.parametrize("module", ["langchain_google_genai", "av"])
def test_optional_deps_remain_absent(module: str) -> None:
    """kernel 的可选功能仍然关着。哪天它变成必需，这条会失败提醒重新决定。

    ## PIL 为什么从这张表里去掉了

    它曾经在列。接记忆功能时选了 FastEmbed 做 embedder（DeepSeek 没有
    embeddings 接口，而 Mem0 的向量检索必须要一个），而 **fastembed 依赖
    pillow** —— 于是 PIL 进了环境，这条断言变红。

    这正是它设计的用途：不是「PIL 不许存在」，而是「它的存在状态变了，
    回来重新决定一次」。重新决定的结论是**功能照旧关着**：

      · kernel 的 video 分支要 `av` + `pillow` **两个都有**才可用，
        而 av 仍然不在 —— 所以 video 依旧不可用，守住它的是 av。
      · PIL 现在只是 fastembed 的图像处理依赖，与 kernel 的任何分支无关。

    所以把 PIL 移出这张表，同时保留 av —— 后者才是真正的闸门。把 PIL
    留着只会让这条测试在每次动 embedder 时莫名其妙地红。
    """
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, f"{module} 意外存在 —— 依赖清单与预期不符"
