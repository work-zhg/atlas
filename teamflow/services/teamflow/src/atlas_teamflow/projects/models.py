"""项目领域的表：tf_project、tf_project_template（绑定）、tf_project_role（按模板给角色分配）。"""

from __future__ import annotations

from uuid import UUID

from atlas_tenancy import TenantOwned
from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Audited, Base, Keys, Stamped
from ..db.types import UUIDType

__all__ = ["Project", "ProjectRole", "ProjectTemplate"]


class Project(TenantOwned, Keys, Audited, Base):
    """属于一个团队，名字在团队内唯一；归档后不能再发起。"""

    __tablename__ = "tf_project"

    team_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    created_by: Mapped[UUID | None] = mapped_column(UUIDType)

    __table_args__ = (
        UniqueConstraint("team_uuid", "name", name="uq_tf_project_name"),
        CheckConstraint("status IN ('active','archived')", name="status_enum"),
    )


class ProjectTemplate(TenantOwned, Keys, Stamped, Base):
    """项目绑定的一个流程模板，跟随它的最新发布版；角色分配挂在绑定下面。"""

    __tablename__ = "tf_project_template"

    project_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_project.uuid", ondelete="CASCADE"), nullable=False
    )
    template_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)

    __table_args__ = (
        UniqueConstraint("project_uuid", "template_uuid", name="uq_tf_project_template"),
    )


class ProjectRole(TenantOwned, Keys, Base):
    """一个模板角色的一个担任者：成员（用户中心 uid）或数字员工（tf_team_agent.uuid）。"""

    __tablename__ = "tf_project_role"

    binding_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_project_template.uuid", ondelete="CASCADE"), nullable=False
    )
    role_key: Mapped[str] = mapped_column(String(32), nullable=False)
    #: user / agent；agent 每个角色最多一个
    assignee_type: Mapped[str] = mapped_column(String(8), nullable=False)
    assignee_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "binding_uuid", "role_key", "assignee_type", "assignee_uuid", name="uq_tf_project_role"
        ),
        CheckConstraint("assignee_type IN ('user','agent')", name="assignee_type_enum"),
        Index("ix_tf_project_role_binding", "binding_uuid", "role_key"),
    )
