"""SkillRef —— 已投送技能的清单条目。

server 在会话创建时把技能拷进 OSS 的 skills 前缀（skill_copy），随后按
这个形态把清单交给 RunHooks.skills；kernel 的 SkillsMiddleware 只负责把
它渲染进系统提示词 —— 不扫描、不读文件。两边共用的形态就住在这里。
"""

from dataclasses import dataclass

__all__ = ["SkillRef"]


@dataclass(frozen=True)
class SkillRef:
    """一个已投送到工作区的技能。

    Attributes:
        name: 技能标识，同时是目录名。
        description: 进系统提示词的那一句。★ 它是**路由依据** —— 模型只凭
            它决定要不要展开正文。写得含糊，技能就等于不存在。
        path: SKILL.md 的绝对路径，模型拿它去 read_file。
    """

    name: str
    description: str
    path: str
