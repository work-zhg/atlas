"""SkillsMiddleware —— 把技能清单渲染进系统提示词。不做发现，不读文件。

## 为什么不扫描

原本的实现在 `before_agent` 里扫技能目录、解析 YAML frontmatter、把结果缓存
进 `SkillsState`。那在它原本的场景里是对的：技能是人手动扔进
`~/.claude/skills/` 的，**没有任何注册表** —— 文件系统就是注册表，扫描是唯一
的发现途径。

我们的架构反过来：注册表先于文件存在。有哪些技能来自 `spec.skills` 里钉死的
`(slug, version)`，name/description 来自配置平面的 skill 表，路径是确定的
`/workspace/.skills/<slug>/SKILL.md`。继承那个扫描，等于假装自己不知道自己
刚拷贝了什么。

而且代价是实打实的：atlas **没有 checkpointer**，每轮 run 都是一次全新的
graph invocation，`SkillsState` 不跨 run 保留 —— 于是**每一轮对话都重扫一遍**。
配 5 个技能的 agent，每轮在模型看到任何东西之前先做 1 次目录列举 + 5 次
SKILL.md 读取，全打在对象存储上。

## 随之消失的东西

扫描、YAML frontmatter 解析、`SkillsState` / `before_agent`、加载警告整套、
多 source 覆盖逻辑与来源标注。原来 1053 行。

name/description/文件大小的**校验搬到配置平面的发布路径** —— 技能作者提交时
就被拒，而不是等某个用户跑到一半在提示词里看到一条警告。与「能在保存时拒绝
的，不留到运行时」一致。

frontmatter 仍然写进会话副本，但它从**运行时的数据源**降为**发布时的输入**：
保留它只为可移植性 —— 会话里的技能目录导出后仍是一个合法技能包。

## 渐进披露不变

系统提示词里只有 name + description + **路径**；正文由模型按需 `read_file`
读取；`scripts/` 之类的附属文件再按正文的指引读或执行。装 20 个技能的常驻
成本仍然只有 20 行描述。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from atlas_engine.contracts.skills import SkillRef

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)

from atlas_engine.kernel.middleware._utils import append_to_system_message

__all__ = ["SKILLS_SYSTEM_PROMPT", "SkillRef", "SkillsMiddleware"]


SKILLS_SYSTEM_PROMPT = """## Skills

You have skills available in this session's workspace. Each one is a directory
with instructions and, sometimes, reference documents and runnable scripts.

**Available skills:**

{skills_list}

**How to use them (progressive disclosure):**

1. Check whether the task matches a skill's description above.
2. Read its full instructions: `read_file(file_path="<path>", limit=1000)`.
   The default limit of 100 lines is too small for most skill files.
3. Follow the workflow it describes. It may point at reference documents or
   scripts in the same directory — use absolute paths for those.

Skills here are **this session's own copies**. You may edit them; the changes
affect this session only.
"""


def _render(skills: Sequence[SkillRef]) -> str:
    lines: list[str] = []
    for skill in skills:
        lines.append(f"- **{skill.name}**: {skill.description}")
        lines.append(f"  -> Read `{skill.path}` for full instructions")
    return "\n".join(lines)


class SkillsMiddleware(AgentMiddleware):
    """把技能清单渲染进系统提示词。

    Args:
        skills: 已投送到工作区的技能。**空列表时不应该装配这个中间件** ——
            装一个内容为空的技能段只会白占提示词；这个判断在调用方
            （`create_deep_agent`）。
        system_prompt: 覆盖默认模板，必须含 `{skills_list}` 占位符。
    """

    def __init__(
        self,
        *,
        skills: Sequence[SkillRef],
        system_prompt: str | None = None,
    ) -> None:
        template = system_prompt or SKILLS_SYSTEM_PROMPT
        if "{skills_list}" not in template:
            msg = "system_prompt must contain the '{skills_list}' placeholder"
            raise ValueError(msg)
        super().__init__()
        self._skills = list(skills)
        self._template = template

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        if not self._skills:
            return request
        block = self._template.format(skills_list=_render(self._skills))
        return request.override(
            system_message=append_to_system_message(request.system_message, block)
        )

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        return handler(self.modify_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        return await handler(self.modify_request(request))
