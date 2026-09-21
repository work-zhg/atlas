# 设计文档

浏览器直接打开 HTML 即可，不需要起服务 —— 全部是静态页、相对链接、
唯一的外部依赖是 `assets/style.css`。入口：[`index.html`](index.html)。

## 分两层

**顶层**是各子系统的设计：一个系统一页，讲清楚它解决什么问题、边界在哪、
哪些做法被否决过以及为什么。

| 文件 | 内容 |
| --- | --- |
| [`atlas-overview.html`](atlas-overview.html) | 项目概览与系统架构（交互式架构图） |
| [`architecture.html`](architecture.html) | 分层与依赖方向 |
| [`agent.html`](agent.html) | Agent 管理：版本快照、能力探测 |
| [`runtime.html`](runtime.html) | 执行环境：Pod、配对、优雅终止 |
| [`execution.html`](execution.html) | 执行器：一轮 run 的生命周期 |
| [`session.html`](session.html) | 会话与消息 |
| [`memory.html`](memory.html) | 记忆与压缩 |
| [`permission.html`](permission.html) | 审批回路 |
| [`skill.html`](skill.html) · [`mcp.html`](mcp.html) | 技能 / MCP 接入 |
| [`observability.html`](observability.html) | 事件流与可观测性 |
| [`native.html`](native.html) · [`local.html`](local.html) | 原生形态 / 本地运行 |

**`detail/`** 是几处需要展开讲的深水区 —— 顶层页给结论，这里给推导过程：

| 文件 | 内容 |
| --- | --- |
| [`detail/acp.html`](detail/acp.html) | ACP 智能体：包规划、时序、落地顺序 |
| [`detail/subagent.html`](detail/subagent.html) | 子智能体：子会话 = thread |
| [`detail/filesystem.html`](detail/filesystem.html) | 会话工作区与对象存储 |
| [`detail/kernel.html`](detail/kernel.html) | kernel 的中间件链 |
| [`detail/native.html`](detail/native.html) · [`detail/structure.html`](detail/structure.html) | 原生执行 / 代码结构 |

[`prototype/`](prototype/index.html) 是界面原型，用来对齐交互，不是实现。

## 这些文档会被修正

**设计先写、实现后跑，跑通之后发现设计错了的地方会回来改文档，并且写明
原来错在哪。** 比如 `detail/acp.html` §12 就挂着一串「真集群跑通后的返工」：
双 subPath 挂不了 FUSE、挂载凭据从没传过、Pod 地址指向一个不存在的
Service、`ensure` 不等就绪、只写 `runAsNonRoot` 会让 Pod 起不来。

保留这些记录是有意的。删掉修正、只留最终形态，会让人以为这些结论是一开始
就想明白的 —— 而下一个人会在同样的地方再摔一次。

## 与仓库的关系

这是私有仓库，设计文档随代码一起入库 —— 改了什么、为什么改、哪一版对应
哪次实现，都能在历史里查到。`.gitignore` 里曾经有
`docs/` / `design-system/` / `prototype/` 三条（注释写着"内部设计文档不随
开源仓库发布"），已经去掉。

仍然被忽略的只剩架构图的**源文件**（`atlas-architecture.drawio` 等）——
那是编辑用的，渲染好的成品是 `atlas-overview.html`。
