# atlas-teamflow

AI TeamFlow：团队、项目、流程协同；人员与权限只走用户中心开放接口，Agent 只走 Atlas 的 HTTP 接口。

## 用户中心登记

代码里只有判断用到的编码，不往用户中心登记。下面这些由运营人员在运营中心「应用目录」里录入；
编码必须和这里一致，名称、角色怎么组合可以按需调整。

**应用**

| 编码 | 名称 | 入口地址 |
|---|---|---|
| `teamflow` | AI TeamFlow | `/teamflow` |

**操作码**

| 模块 | 编码 | 名称 |
|---|---|---|
| 流程模板 | `flow_template:view` | 查看流程模板 |
| 流程模板 | `flow_template:manage` | 维护流程模板 |
| 文件模板 | `file_template:view` | 查看文件模板 |
| 文件模板 | `file_template:manage` | 维护文件模板 |
| 团队 | `team:create` | 新建团队 |
| 团队 | `team:edit` | 编辑团队信息 |
| 团队 | `team:member` | 管理团队成员 |
| 团队 | `team:agent` | 接入 / 移除团队 Agent |
| 项目 | `project:create` | 新建项目 |
| 项目 | `project:configure` | 项目设置 |
| 项目 | `project:archive` | 归档项目 |
| 流程 | `process:view` | 查看项目与流程 |
| 流程 | `process:start` | 发起流程 |
| 流程 | `process:handle` | 节点协同 |
| 流程 | `process:review` | 准入 / 准出评审 |
| 流程 | `process:terminate` | 终止流程 |
| 系统 | `settings:manage` | 系统设置 |

**菜单**（前端按路径显示，编码、路径要和前端一致；`teams` 是公共菜单）

| 编码 | 类型 | 名称 | 图标 | 路径 | 上级 |
|---|---|---|---|---|---|
| `todo` | 菜单 | 待我处理 | 📥 | `/todo` | |
| `teams` | 菜单（公共） | 团队 | 👥 | `/teams` | |
| `sys` | 目录 | 系统设置 | ⚙️ | | |
| `flow_tpl` | 菜单 | 流程模板 | 🧭 | `/templates/flow` | `sys` |
| `file_tpl` | 菜单 | 文件模板 | 📄 | `/templates/file` | `sys` |
| `settings` | 菜单 | 集成配置 | 🔌 | `/settings` | `sys` |
| `jobs` | 菜单 | 定时任务 | ⏱ | `/jobs` | `sys` |

**数据编码**

| 编码 | 名称 | 说明 |
|---|---|---|
| `Team` | 团队 | 一个团队一条数据权限：成员读写，团队管理员 Owner |

**建议的内置角色**

| 编码 | 名称 | 操作码 | 菜单 |
|---|---|---|---|
| `TF_PLATFORM_ADMIN` | 平台管理员 | `flow_template:*`、`file_template:*`、`team:create`、`team:member`、`settings:manage` | 系统设置下全部 |
| `TF_TEAM_ADMIN` | 团队管理员 | `team:edit`、`team:member`、`team:agent`、`project:*`、`flow_template:view`、`file_template:view`、`process:view` | |
| `TF_TEAM_MEMBER` | 团队成员 | `process:*`、`file_template:view` | 待我处理 |

调整任意团队的成员与管理员 = `team:member` ∧ 对该团队 Owner；「对全部团队 Owner」是数据权限（「全部团队 · Owner」），在租户的用户中心里授。
