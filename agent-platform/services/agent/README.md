# atlas-server

Atlas 的服务端：智能体、会话、执行器、沙箱、运营接口。

## 用户中心登记

代码里只有判断用到的编码（`atlas_agent/auth/catalog.py`），不往用户中心登记。下面这些由运营人员
在运营中心「应用目录」里录入；编码必须和这里一致，名称、模块、角色怎么组合可以按需调整。
知识库服务也按 Atlas 的编码判断（它属于 Atlas 应用），一并列在这里。

**应用**

| 编码 | 名称 | 入口地址 |
|---|---|---|
| `atlas` | Atlas | `/atlas` |

**操作码**

| 模块 | 编码 | 名称 | 谁在判断 |
|---|---|---|---|
| 对话 | `chat:use` | 使用对话 | 网页导航 |
| 智能体 | `agent:create` | 新建智能体 | atlas-server |
| 知识库 | `kb:create` | 新建知识库 | 知识库服务、网页 |
| 租户 | `settings:manage` | 租户设置（默认智能体、组织指令、系统功能模型、每人上限） | atlas-server |

**菜单**（前端按路径显示，编码、路径要和前端一致）

| 编码 | 名称 | 图标 | 路径 |
|---|---|---|---|
| `chat` | 对话 | 💬 | `/chat` |
| `agents` | 智能体 | 🤖 | `/agents` |
| `memories` | 记忆 | 🧠 | `/memories` |
| `knowledge` | 知识库 | 📚 | `/knowledge` |

**数据编码**

| 编码 | 名称 | 说明 |
|---|---|---|
| `AtlasAgent` | 智能体 | 一个智能体一条：使用（READ）/ 编辑（WRITE）/ 所有者（OWNER） |
| `AtlasKnowledgeBase` | 知识库 | 一个知识库一条：检索（READ）/ 编辑文档（WRITE）/ 所有者：删除、共享（OWNER） |

**建议的内置角色**

| 编码 | 名称 | 操作码 | 菜单 |
|---|---|---|---|
| `ATLAS_MEMBER` | 成员 | `chat:use` | 对话、智能体、记忆 |
| `ATLAS_BUILDER` | 构建者 | 成员 + `agent:create`、`kb:create` | 再加知识库 |
| `ATLAS_ADMIN` | 管理员 | 全部 | 全部 |

「管理全部智能体 / 知识库」不是操作码：在租户的用户中心里授数据权限「全部智能体 / 全部知识库 · Owner」。
