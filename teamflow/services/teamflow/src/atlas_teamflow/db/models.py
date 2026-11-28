"""全部表的汇总：迁移环境和测试按 Base.metadata 找表，导入这里就把各领域的表都登记上。"""

from __future__ import annotations

from ..audit import AuditEvent
from ..collab.models import Instruction, NodeSession
from ..inbox.models import ChannelBinding, Notification, NotifyCursor, TeamChannel
from ..market.models import MarketCopy, MarketTemplate, MarketVersion
from ..process.models import Process, ProcessEvent, ProcessNode, ReviewRound, Submission, Vote
from ..projects.models import Project, ProjectRole, ProjectTemplate
from ..space.models import FlowSpace
from ..teams.models import Team, TeamAgent
from ..templates.models import FlowTemplate, TemplateDraft, TemplateVersion
from .base import Base

__all__ = [
    "AuditEvent",
    "Base",
    "ChannelBinding",
    "FlowSpace",
    "FlowTemplate",
    "Instruction",
    "MarketCopy",
    "MarketTemplate",
    "MarketVersion",
    "NodeSession",
    "Notification",
    "NotifyCursor",
    "Process",
    "ProcessEvent",
    "ProcessNode",
    "Project",
    "ProjectRole",
    "ProjectTemplate",
    "ReviewRound",
    "Submission",
    "Team",
    "TeamAgent",
    "TeamChannel",
    "TemplateDraft",
    "TemplateVersion",
    "Vote",
]
