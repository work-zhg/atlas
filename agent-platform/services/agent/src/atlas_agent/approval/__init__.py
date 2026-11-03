"""审批组件（c4-model/agent/code-approval.html）。"""

from .channel import DecisionChannel
from .service import ApprovalService, DecisionListener, OnlineApprovals, RunApprovalGate

__all__ = [
    "ApprovalService",
    "DecisionChannel",
    "DecisionListener",
    "OnlineApprovals",
    "RunApprovalGate",
]
