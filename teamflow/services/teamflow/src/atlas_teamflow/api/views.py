"""接口的输出形状：表行 → JSON。uuid 一律转字符串，时间是 ISO 8601（UTC）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from ..collab.models import Instruction, NodeSession
from ..inbox.models import Notification
from ..market.models import CATEGORIES, MarketTemplate, MarketVersion
from ..process.models import Process, ProcessEvent, ProcessNode, ReviewRound, Submission, Vote
from ..projects.models import Project
from ..teams.agents import AgentCard
from ..teams.models import Team, TeamAgent
from ..templates.models import FlowTemplate, TemplateDraft, TemplateVersion

__all__ = [
    "agent_out",
    "card_out",
    "event_out",
    "instruction_out",
    "market_out",
    "node_out",
    "notification_out",
    "process_out",
    "project_out",
    "review_out",
    "session_out",
    "submission_out",
    "team_out",
    "template_out",
]


def _t(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def _u(v: UUID | None) -> str | None:
    return str(v) if v else None


def team_out(t: Team, level: str | None = None) -> dict[str, Any]:
    return {
        "id": str(t.uuid),
        "name": t.name,
        "description": t.description,
        "status": t.status,
        "version": t.version,
        "my_level": level,
        "created_at": _t(t.created_at),
    }


def agent_out(a: TeamAgent, stats: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": str(a.uuid),
        "atlas_agent_id": a.atlas_agent_id,
        "name": a.name,
        "kind": a.kind,
        "model": a.model,
        "description": a.description,
        "available": a.available,
        "added_by": str(a.added_by),
        "synced_at": _t(a.synced_at),
        "stats": stats or {"working": 0, "passed": 0, "avg_rounds": None},
    }


def card_out(c: AgentCard) -> dict[str, Any]:
    return {
        "atlas_agent_id": c.atlas_agent_id,
        "name": c.name,
        "kind": c.kind,
        "model": c.model,
        "description": c.description,
        "skills": c.skills,
        "available": c.available,
    }


def template_out(
    t: FlowTemplate,
    draft: TemplateDraft | None = None,
    versions: list[TemplateVersion] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(t.uuid),
        "team_id": str(t.team_uuid),
        "name": t.name,
        "description": t.description,
        "icon": t.icon,
        "status": t.status,
        "latest_version_no": t.latest_version_no,
        "origin": (
            {"market_template_id": str(t.origin_market_uuid), "version": t.origin_market_version}
            if t.origin_market_uuid
            else None
        ),
        "updated_at": _t(t.updated_at),
    }
    if draft is not None:
        out["draft"] = {
            "structure": draft.structure,
            "rev": draft.rev,
            "base_version_no": draft.base_version_no,
            "editor": _u(draft.editor),
            "updated_at": _t(draft.updated_at),
        }
    if versions is not None:
        out["versions"] = [
            {
                "id": str(v.uuid),
                "version_no": v.version_no,
                "note": v.note,
                "published_by": str(v.published_by),
                "published_at": _t(v.published_at),
            }
            for v in versions
        ]
    return out


def market_out(
    t: MarketTemplate,
    latest: MarketVersion | None = None,
    *,
    maintainer: bool = False,
    stats: tuple[int, int] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(t.uuid),
        "name": t.name,
        "category": t.category,
        "category_name": CATEGORIES.get(t.category, t.category),
        "description": t.description,
        "who": t.who,
        "icon": t.icon,
        "color": t.color,
        "status": t.status,
        "latest_version": t.latest_version,
    }
    if latest is not None:
        out["structure"] = latest.structure
        out["published_at"] = _t(latest.published_at)
    if maintainer:
        out["draft"] = t.draft
        out["version"] = t.version
    if stats is not None:
        out["copies"], out["tenants"] = stats
    return out


def project_out(p: Project) -> dict[str, Any]:
    return {
        "id": str(p.uuid),
        "team_id": str(p.team_uuid),
        "name": p.name,
        "description": p.description,
        "status": p.status,
        "version": p.version,
        "created_at": _t(p.created_at),
    }


def process_out(p: Process) -> dict[str, Any]:
    return {
        "id": str(p.uuid),
        "no": p.no,
        "code": f"F-{p.no}",
        "title": p.title,
        "team_id": str(p.team_uuid),
        "project_id": str(p.project_uuid),
        "template_id": str(p.template_uuid),
        "template_version_id": str(p.template_version_uuid),
        "space_id": str(p.space_uuid),
        "status": p.status,
        "started_by": str(p.started_by),
        "started_at": _t(p.created_at),
        "finished_at": _t(p.finished_at),
    }


def node_out(n: ProcessNode) -> dict[str, Any]:
    return {
        "id": n.node_id,
        "name": n.name,
        "exec_role": n.exec_role,
        "status": n.status,
        "round": n.round,
        "atlas_agent_id": n.atlas_agent_id,
        "started_at": _t(n.started_at),
        "passed_at": _t(n.passed_at),
    }


def submission_out(s: Submission) -> dict[str, Any]:
    return {
        "id": str(s.uuid),
        "round": s.round,
        "paths": list(s.paths),
        "submitted_by": str(s.submitted_by),
        "submitted_at": _t(s.created_at),
    }


def review_out(r: ReviewRound, votes: list[Vote]) -> dict[str, Any]:
    return {
        "id": str(r.uuid),
        "stage": r.stage,
        "round": r.round,
        "roles": list(r.roles),
        "rule": r.rule,
        "reviewers": list(r.reviewers),
        "submissions": list(r.submissions),
        "status": r.status,
        "opened_at": _t(r.created_at),
        "closed_at": _t(r.closed_at),
        "votes": [
            {
                "reviewer": str(v.reviewer),
                "decision": v.decision,
                "comment": v.comment,
                "return_to": v.return_to,
                "at": _t(v.created_at),
            }
            for v in votes
        ],
    }


def session_out(ns: NodeSession | None, *, can_send: bool) -> dict[str, Any]:
    if ns is None:
        return {"thread_id": None, "atlas_agent_id": None, "state": None, "can_send": False}
    return {
        "thread_id": ns.atlas_thread_id,
        "atlas_agent_id": ns.atlas_agent_id,
        "state": ns.state,
        "taken_by": _u(ns.taken_by),
        "last_run_id": ns.last_run_id,
        "can_send": can_send,
    }


def instruction_out(i: Instruction) -> dict[str, Any]:
    return {
        "id": str(i.uuid),
        "kind": i.kind,
        "round": i.round,
        "author": _u(i.author),
        "status": i.status,
        "run_id": i.atlas_run_id,
        "sent_at": _t(i.sent_at),
        "last_error": i.last_error,
        "created_at": _t(i.created_at),
    }


def event_out(e: ProcessEvent) -> dict[str, Any]:
    data = {k: v for k, v in e.data.items() if k != "to"}
    return {
        "seq": e.seq,
        "type": e.type,
        "node_id": e.node_id,
        "actor": _u(e.actor),
        "data": data,
        "at": _t(e.created_at),
    }


def notification_out(n: Notification) -> dict[str, Any]:
    return {
        "id": str(n.uuid),
        "text": n.text,
        "link": n.link,
        "read": n.read_at is not None,
        "at": _t(n.created_at),
    }
