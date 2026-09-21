"""抽取指令（记忆设计 §04 的落地校正）。

第一轮只做抽取与存储、纯观察质量 —— 观察的结论是三条必须在开自动注入
**之前**解决的问题。本模块解决其中两条，靠的是 mem0 的
`custom_instructions`（它在 additive extraction 的提示词里被标为
「User-defined rules (highest priority)」）。

## 问题 ②：助手自己说的话被当成「关于用户的事实」

实测记下来的东西里有：

    Assistant confirmed it will use bun commands going forward …
    Assistant recommended persisting the bun preference in a durable file —
    options given were a README.md or CONTRIBUTING.md note …

第二条是一整段。这不是 mem0 出错，是它**刻意**的行为 —— 内置提示词写着
「You extract from BOTH user and assistant messages. Assistant messages
contain recommendations, plans, suggestions…」。

但那与本平台的定位不符：§01 说记忆是「关于**用户**与其工作的结构化事实」。
助手的建议与自述不是关于用户的事实，它们会稀释记忆库，将来开注入时还要
白占上下文去复述助手自己说过的话。

## 问题 ③：对话是中文，记忆却是英文

mem0 的提示词里有现成的 `use_input_language` 开关，措辞相当完备，但
**main.py 根本没把它传下去**（写死为 False）。所以只能把同样的要求放进
custom_instructions —— 下面这段的措辞就是从 mem0 自己那段抄来的，保持
与它内置提示词同一套说法。

## 为什么不是改成「只喂 user 消息」

那样确实能根治 ②，但会丢掉 §04 想要的「结论与达成的共识」—— 比如助理
跑了一次 make test 之后在正文里说「这个仓库的测试命令是 make test」。
用指令限定「只记关于用户的事实」，比直接砍掉一整个输入源更精准。
"""

from __future__ import annotations

__all__ = ["EXTRACTION_INSTRUCTIONS"]

EXTRACTION_INSTRUCTIONS = """\
## 记录范围

只记录**关于用户及其工作的事实**：偏好、习惯、约定、项目信息、长期成立的
约束。判据是「下次会话里不说也应该成立」。

不要记录下列内容，即使它们出现在助手的消息里：
- 助手自己的计划、承诺与确认（如「我接下来会用 bun 命令」）
- 助手给出的建议与选项清单（如「建议把偏好写进 README 或 package.json」）
- 助手对本次任务的复述与总结

例外：助手在正文里**陈述的客观事实**可以记，但要改写成关于用户或其项目的
陈述。例如助手说「我运行了 make test，这是该仓库的测试命令」，应当记成
「该项目的测试命令是 make test」，而不是「助手运行了 make test」。

## 事实变更

当一条新事实与已有记忆冲突时（例如用户从 pnpm 改用 bun），新记忆里要把
变更说清楚，包含新值与被取代的旧值。

## 语言

CRITICAL: Respond in the SAME LANGUAGE and SCRIPT as the input messages.
输入是中文就用中文提炼，不要翻译成英文。技术术语、专有名词与品牌名保持
输入中的原样（pnpm、bun、make test 等不要翻译）。
"""
