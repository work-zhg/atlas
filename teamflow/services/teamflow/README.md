# atlas-teamflow

AI TeamFlow：面向团队协作的 AI 工作流平台。团队把「先做什么、再做什么、谁来把关」写成流程模板，项目给模板角色配成员和数字员工；每个节点是一个 Agent 平台的数字员工会话，人在准出、准入时评审。设计见 `c4-model/teamflow/design-teamflow.html`。

人员与权限只走用户中心开放接口，智能体、会话、流程空间只走 Agent 平台的 HTTP 接口；和 atlas 其它业务包互不 import（根 pyproject 的 import-linter 守着）。

## 命令

| 命令 | 说明 |
|---|---|
| `python -m atlas_teamflow migrate` | 升级 TeamFlow 库（本服务的表 + 运营接口库的表） |
| `python -m atlas_teamflow serve` | 起 HTTP 服务（默认 :8040）；进程内跑投递器（开 run、取消 run）和通知器 |

## 用户中心登记

代码里只有判断用到的编码，不往用户中心登记。下面这些由运营人员在运营中心「应用目录」里录入；编码必须和这里一致，名称、角色怎么组合可以按需调整。

**应用**

| 编码 | 名称 | 入口地址 |
|---|---|---|
| `teamflow` | AI TeamFlow | `/teamflow-ui` |

系统租户也要开通 TeamFlow：运营管理员在系统租户里维护流程市场（TeamFlow 经开放接口 `GET /open/v1/tenants` 的 `is_system` 认系统租户）。

**操作码**（系统角色决定「能做哪类事」，对哪个团队做看团队数据权限）

| 模块 | 编码 | 名称 | 还要的团队级别 |
|---|---|---|---|
| 团队 | `team:create` | 新建团队 | — |
| 团队 | `team:config` | 团队配置（成员、数字员工、通知渠道） | Owner |
| 流程 | `flow:config` | 流程配置（流程模板、项目、给角色分配） | Owner |
| 流程 | `process:run` | 发起、执行、评审、上传、终止 | 读写 |
| 流程 | `process:view` | 看流程、流程空间、追溯 | 只读 |

**数据编码**

| 编码 | 名称 | 说明 |
|---|---|---|
| `Team` | 团队 | 一个团队一条数据：Owner = 团队管理员，读写 = 成员，只读 = 只能看；「全部团队」编码级授权 = 管本租户全部团队 |
| `FlowMarket` | 流程市场 | 只用编码级授权：系统租户里有「读写」的人是运营管理员，能维护流程市场 |

**建议的内置角色**

| 编码 | 名称 | 操作码 |
|---|---|---|
| `TF_TEAM_ADMIN` | 团队管理员 | `team:create`、`team:config`、`flow:config`、`process:run`、`process:view` |
| `TF_TEAM_MEMBER` | 团队成员 | `process:run`、`process:view` |

运营管理员不加角色：系统租户里的人，授「流程市场」编码级读写，再给 `TF_TEAM_MEMBER` 让他能登录 TeamFlow。

★ 模板角色（产品、开发……）是模板自己的概念，在模板的角色表里定义、在项目里分配给人，和这里的系统角色没有关系。

## 依赖 Agent 平台的接口

| 用途 | 接口 |
|---|---|
| 数字员工 | `GET /v1/agents`、`GET /v1/agents/{id}`、`GET/POST /v1/agents/{id}/grants`（以接入人的身份授「使用」） |
| 流程空间 | `POST /v1/app-workspaces`（按编号幂等）、`DELETE /v1/app-workspaces/{编号}`、`…/files`（列、下载、上传、查） |
| 节点会话 | `POST /v1/threads`（`app_workspace` = 流程空间）、`POST /v1/threads/{id}/runs`（幂等键 = 指令号）、`POST /v1/runs/{id}/cancel` |

节点页的对话组件直连 Agent 平台（看对话、回答、浏览器接管、停止），不经 TeamFlow；只有开新 run 经 TeamFlow（`POST /api/v1/processes/{流程}/nodes/{节点}/messages`）。

## 测试

```bash
cd teamflow/services/teamflow
uv run pytest tests        # 独立测试库 atlas_teamflow_test；用户中心、Agent 平台用内存实现（tests/fakes.py）
```
