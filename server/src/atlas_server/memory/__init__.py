"""跨会话记忆（记忆设计）。

本轮只做**写入与存储**，不向任何 Agent 暴露 —— 这是文档 §12 明确的上线
顺序，理由是记忆有个不对称风险：记对了是锦上添花，**记错了是持续伤害**。
一条被错误提炼的「事实」会在之后每一轮被注入，让模型反复基于错误前提
作答，而用户往往不知道问题出在哪。

所以先纯观察提炼质量（靠 GET /v1/memories 看），达标之后再开检索工具，
最后才开自动注入。检索工具与注入中间件本轮**都不存在**。
"""

from __future__ import annotations

from .client import MemoryClient, make_memory
from .converge import converge
from .extractor import ExtractionJob, build_job
from .queue import DEAD_KEY, QUEUE_KEY, enqueue, run_worker

__all__ = [
    "DEAD_KEY",
    "QUEUE_KEY",
    "ExtractionJob",
    "MemoryClient",
    "build_job",
    "converge",
    "enqueue",
    "make_memory",
    "run_worker",
]
