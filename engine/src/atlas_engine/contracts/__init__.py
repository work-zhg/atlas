"""能力协议层 —— 整个系统最底部、谁都不拥有的那一层。

## 定位

三个能力协议（Filesystem / Sandbox / Delegation）与它们的结果类型**定义在
这里**，kernel 与 server 都向下依赖本层：

    server（实现方）   OssFilesystem / DockerSandbox / SubagentService
        ↓ 实现                          ↑ 消费
    contracts（本层）  ←──────  kernel 中间件 / engine 装配

## 为什么定义在这里，而不是 kernel

kernel 已被深度改造（裁掉一半、重写技能与子智能体语义），并且**不会被整体
替换** —— 它是持续重写、逐步吸收的内部层，不是可插拔的第三方件。既然如此，
「协议住在 kernel、外面 re-export」就只是给一个不存在的替换场景付抽象税。

本项目要的是**可扩展性**：加一个新能力 = 在这里加一个协议文件 + RunHooks
加一个字段 + server 给一个实现 —— 不动任何架构。协议住在最底层是这个
增长模式的前提：新协议不需要先问「kernel 里放哪」。

## 方向纪律（有守卫）

  · kernel 对外层的 import **只许指向本层**（tests/test_engine_purity.py）
  · server 不得 import atlas_engine.kernel（import-linter + 同一个测试文件）
  · 本层不 import kernel / server / engine 外层的任何东西 —— 它是叶子

错误分类学（errors.py）也在此：`kind` 是 run.failed 事件的公共词汇。

## 收录标准

每一个名字都是一份跨层承诺，动它要过 review。工具函数（create_file_data /
slice_read_response）也在列：它们是读窗口分页语义的参考实现，与
`ReadResult.__post_init__` 的校验是同一份语义的两半。
"""

from .approval import ApprovalGate, Decision
from .delegation import DelegationProtocol
from .errors import (
    REFUSAL_STOP_REASONS,
    ContextOverflow,
    EngineError,
    InvalidSpec,
    LimitExceeded,
    ModelRateLimited,
    ModelRefused,
    ModelUnavailable,
    RunCancelled,
    RunTimeout,
    UnsupportedProvider,
    classify,
)
from .filesystem import (
    EDIT_MAX_BYTES,
    SEARCH_MAX_KEYS,
    DeleteResult,
    EditResult,
    FileData,
    FilesystemProtocol,
    ReadResult,
    SearchResult,
    WriteResult,
    create_file_data,
    file_data_to_string,
    normalize_read_bounds,
    slice_read_response,
)
from .sandbox import (
    DEFAULT_WORKDIR,
    ExecuteArtifact,
    ExecuteResponse,
    SandboxProtocol,
)
from .skills import SkillRef

__all__ = [
    "DEFAULT_WORKDIR",
    "EDIT_MAX_BYTES",
    "REFUSAL_STOP_REASONS",
    "SEARCH_MAX_KEYS",
    "ApprovalGate",
    "ContextOverflow",
    "Decision",
    "DelegationProtocol",
    "DeleteResult",
    "EditResult",
    "EngineError",
    "InvalidSpec",
    "LimitExceeded",
    "ModelRateLimited",
    "ModelRefused",
    "ModelUnavailable",
    "RunCancelled",
    "RunTimeout",
    "UnsupportedProvider",
    "ExecuteArtifact",
    "ExecuteResponse",
    "FileData",
    "FilesystemProtocol",
    "ReadResult",
    "SandboxProtocol",
    "SearchResult",
    "SkillRef",
    "WriteResult",
    "classify",
    "create_file_data",
    "file_data_to_string",
    "normalize_read_bounds",
    "slice_read_response",
]
