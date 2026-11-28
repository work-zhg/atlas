"""共享内核：流程结构（c4-model/teamflow/code-templates.html 的 FlowStructure）。

流程模板、流程市场、流程运行共用；纯函数，不依赖任何领域，也不碰数据库。
"""

from .flow import (
    Issue,
    ancestors,
    deps,
    descendants,
    downstreams,
    exec_roles,
    new_structure,
    node_ids,
    normalize,
    review_roles,
    roots,
    validate,
)

__all__ = [
    "Issue",
    "ancestors",
    "deps",
    "descendants",
    "downstreams",
    "exec_roles",
    "new_structure",
    "node_ids",
    "normalize",
    "review_roles",
    "roots",
    "validate",
]
