"""ReviewPolicy：上传、审查的规则。

★ 租户上传的一律经本租户的审核人人工审查，没有例外：技能里的脚本会在沙箱里执行，描述会进模型的
  上下文，机械检查必然漏报。多租户以后没有「内置」上传（原来作者选 builtin 就能跳过审查）：产品
  自带的技能发布到技能市场，租户从市场拷贝。
★ 审查人不能是上传人（四眼原则）；本机单人开发可以打开 allow_self_review。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..db.models import Skill
from ..errors import Conflict, Forbidden, Invalid

__all__ = ["ReviewPolicy"]

#: 租户能上传的来源
UPLOAD_SOURCES = ("tenant", "imported")


@dataclass(frozen=True)
class ReviewPolicy:
    allow_self_review: bool = False

    def check_source(self, source: str, origin_url: str | None) -> None:
        if source == "builtin":
            raise Invalid("产品自带的技能发布到技能市场（运营中心），不能作为租户技能上传")
        if source not in UPLOAD_SOURCES:
            raise Invalid(f"未知来源 {source!r}")
        if source == "imported" and not origin_url:
            raise Invalid("imported 技能必须给出出处（origin_url）")

    def check_new_version(self, skill: Skill, source: str) -> None:
        """已有的技能：之后的版本必须同一来源；从市场装的只能从市场装新版本。"""
        if skill.source == "market":
            raise Conflict(f"技能 {skill.slug} 是从技能市场安装的，新版本请从技能市场安装")
        if skill.source != source:
            raise Conflict(f"技能 {skill.slug} 的来源是 {skill.source}，不能以 {source} 上传新版本")

    def check_reviewer(self, reviewer: str, uploader: str) -> None:
        if reviewer == uploader and not self.allow_self_review:
            raise Forbidden("审查人不能是上传人（四眼原则）")
