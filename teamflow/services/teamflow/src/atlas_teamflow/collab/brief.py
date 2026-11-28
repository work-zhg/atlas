"""BriefRenderer：把工作单写成给数字员工的话。

工作说明；上游最近一次提交的文件路径（不拷贝、不带上游的对话）；约定产出写在「{节点名}/」下、
不改上游提交的文件（有问题就提）；驳回或打回的意见。怎么措辞随智能体和技能调整，和状态机无关。
"""

from __future__ import annotations

from ..process.port import WorkOrder

__all__ = ["render", "render_handback"]


def render(order: WorkOrder) -> str:
    parts: list[str] = []
    if order.round > 1 and order.feedback:
        parts.append(
            f"这是「{order.node_name}」的第 {order.round} 轮。评审意见：\n{order.feedback.strip()}"
        )
        parts.append("请按意见修改，改完说明改了什么。")
    elif order.round > 1:
        parts.append(
            f"这是「{order.node_name}」的第 {order.round} 轮：上游重新提交了，"
            "请按新的输入修订你的产出。"
        )
    if order.instructions.strip():
        parts.append(("工作说明：\n" if order.round > 1 else "") + order.instructions.strip())
    if order.upstream:
        lines = [f"- {path}（{name}）" for name, paths in order.upstream for path in paths]
        parts.append("上游提交的文件（在工作目录里，直接读）：\n" + "\n".join(lines))
    parts.append(
        f"产出写在工作目录的「{order.node_name}/」下。上游提交的文件不要改，觉得有问题就提出来。"
    )
    return "\n\n".join(parts)


def render_handback(node_name: str, note: str) -> str:
    note = note.strip() or "（没有写说明）"
    return (
        f"执行人接管了一会儿，直接改了流程空间里的文件，现在交还给你。执行人的说明：\n{note}\n\n"
        f"先看一下「{node_name}/」和相关文件的当前内容，从人改过的地方接着做。"
    )
