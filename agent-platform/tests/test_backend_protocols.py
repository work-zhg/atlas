"""能力协议的边界守卫。

这些断言红了不是功能坏了，是**分层破了**：`FilesystemProtocol` 与
`SandboxProtocol` 是刻意**并列**的，一旦有人把文件操作加回沙箱侧（或反过来
让文件系统去跑命令），继承关系就会悄悄长回来 —— 而那正是拆分要消除的东西。

协议现已定义在 `atlas_engine.contracts`（不再住 kernel）：kernel 与 server
都向下依赖它，方向纪律由 test_engine_purity.py 看守。
"""

from __future__ import annotations

import importlib
import inspect

import pytest

from atlas_engine.contracts import (
    DEFAULT_WORKDIR,
    EDIT_MAX_BYTES,
    SEARCH_MAX_KEYS,
    FilesystemProtocol,
    SandboxProtocol,
    SearchResult,
)


def _method_names(proto: type) -> set[str]:
    return {
        name
        for name, member in inspect.getmembers(proto)
        if not name.startswith("_") and (inspect.isfunction(member) or isinstance(member, property))
    }


def test_protocols_are_disjoint() -> None:
    """两个协议不共享任何方法名。

    共享方法名是继承关系长回来的第一个征兆：一旦沙箱又提供了 read/write，
    调用方就会开始「传哪个都行」，接缝随即失效。
    """
    overlap = _method_names(FilesystemProtocol) & _method_names(SandboxProtocol)
    assert overlap == set(), f"两个协议出现了共享方法：{sorted(overlap)}"


def test_filesystem_protocol_surface() -> None:
    """文件协议只声明对象存储做得到的事。

    ★ 没有 grep：OSS 不支持内容检索，按内容查找归 execute。
    ★ 没有 ls/glob：统一成 search（前缀 + 文件名 glob）。
      加回它们意味着实现方要在对象存储上模拟目录语义，而那正是
      `search` 这个折中要避免的。
    """
    names = _method_names(FilesystemProtocol)
    assert names == {
        "read",
        "aread",
        "search",
        "asearch",
        "write",
        "awrite",
        "edit",
        "aedit",
        "delete",
        "adelete",
    }, sorted(names)


def test_sandbox_protocol_surface() -> None:
    """沙箱协议只负责执行。

    `BaseSandbox` 曾用 execute() 实现 13 个文件操作 —— 每次 read_file
    就是一次 docker exec。这个断言防止那批委托被重新引入。
    """
    names = _method_names(SandboxProtocol)
    assert names == {"id", "execute", "aexecute"}, sorted(names)


def test_sandbox_protocol_is_structural_not_inherited() -> None:
    """满足沙箱协议只需要三个方法，不需要继承任何基类。

    Docker 实现已整体删除（K8s Pod 实现未接入），所以这里用一个最小
    实现替代 —— 断言的本来也不是「Docker 能用」，而是**协议是结构性的**：
    旧的 BaseSandbox / BackendProtocol / SandboxBackendProtocol 那套继承
    关系不许长回来，且沙箱不得兼任文件系统。
    """

    class MinimalSandbox:
        @property
        def id(self) -> str:
            return "probe"

        def execute(self, command, *, timeout=None, cwd=None): ...
        async def aexecute(self, command, *, timeout=None, cwd=None): ...

    box = MinimalSandbox()
    assert isinstance(box, SandboxProtocol)
    assert not isinstance(box, FilesystemProtocol)


def test_search_result_defaults_are_safe() -> None:
    """空结果不是 None —— 调用方不该为「没找到」写一条判空分支。"""
    empty = SearchResult()
    assert empty.keys == [] and empty.common_prefixes == []
    assert empty.truncated is False and empty.error is None


def test_limits_are_pinned() -> None:
    """两个上限是有论证的取值，改动要走 review。

    EDIT_MAX_BYTES: 对象存储不能原地改，一次 edit 必然取回全文。
    SEARCH_MAX_KEYS: 递归列举大前缀会同时打爆上下文与响应时间。
    DEFAULT_WORKDIR: 它同时是 FUSE 挂载点，改这里要连 Pod 模板一起改。
    """
    assert EDIT_MAX_BYTES == 2 * 1024 * 1024
    assert SEARCH_MAX_KEYS == 1000
    assert DEFAULT_WORKDIR == "/workspace"


def test_old_protocol_module_is_gone() -> None:
    """旧协议整体删除，不留向后兼容的转发层。

    ★ 留转发层的话「协议在哪」会长期有两个答案。BackendProtocol 在
      search 取代 ls/glob/grep 之后已经没有实现者；SandboxBackendProtocol
      与 SandboxProtocol 完全重复；BaseSandbox 的 13 个文件委托删光之后
      只剩一个零消费者的 execute_with_offload。
    """
    for gone in (
        # 协议搬去 contracts、实现类删净之后，backends 包整体解散
        #（utils 并入 middleware/_fs_utils）；_api 的弃用告警机制随
        # deepagents 兼容路径一起删除 —— kernel 没有外部用户。
        "atlas_engine.kernel.backends",
        "atlas_engine.kernel._api",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(gone)


def test_result_types_live_with_their_protocol() -> None:
    """结果类型跟着各自的协议走，依赖方向不反转。

    拆分刚做完时它们还留在 protocol.py，于是新协议反过来 import 旧模块 ——
    那条边一直拦着 protocol.py 的删除。搬进 contracts 后同理：结果类型与
    协议同文件，实现方 import 一处就够。
    """
    from atlas_engine.contracts import filesystem as fsp
    from atlas_engine.contracts import sandbox as sbp

    assert {"ReadResult", "WriteResult", "EditResult", "DeleteResult", "FileData"} <= set(fsp.__all__)
    assert {"ExecuteResponse", "ExecuteArtifact"} <= set(sbp.__all__)
