# AI TeamFlow（atlas-teamflow）

让 AI Agent 作为团队成员参与研发流程。人员、角色、操作码、菜单、团队数据权限都来自**用户中心开放接口**；Agent 来自 **Atlas** 的 HTTP 接口（只引用，不创建）。

设计文档：`prototype/teamflow/docs/`（总体设计、权限设计、团队 / 流程模板 / 文件模板设计、流程运行设计 `process-design.html`）。

已实现：

- **配置侧** —— 文件模板、流程模板（草稿 / 校验 / 发布 / 版本）、团队（成员、Agent）、项目（绑定模板、角色分配）；
- **运行侧** —— 发起流程、节点状态机与自动流转、人机协同（指令经 Atlas 会话发给 Agent，输出实时推送）、产物版本（Git 提交）、准出 / 准入评审、驳回与打回、终止、待我处理。

## 运行

```bash
make up                         # 会建 atlas_teamflow 库
make tf-migrate
make uc-serve                   # 依赖用户中心（:8030）
make tf-register-uc ACCOUNT=admin
                                # 登记 TeamFlow 的操作 / 菜单 / 角色 / 数据编码 Team，
                                # 首次输出 App Key / Secret → 写入 .env：
                                #   ATLAS_TF_UC_APP_KEY=…  ATLAS_TF_UC_APP_SECRET=…
                                # 幂等，可重复执行；已存在时加 --rotate 才换 Secret
make tf-web-install
make tf-dev                     # 后端 :8040 + 前端 :3200（/api 由 Next 反向代理）
```

然后在用户中心给人授角色：`TF_PLATFORM_ADMIN`（平台管理员）、`TF_TEAM_ADMIN`（团队管理员）、`TF_TEAM_MEMBER`（团队成员，建议授给根部门含下级）。能进哪个团队由团队数据权限决定（在 TeamFlow 里新建团队、加成员时写入）。

### 多实例部署

```bash
# .env（凭据只放这里，不进代码）
ATLAS_TF_GIT_BACKEND=gitee              # 产物经 Gitee API 提交，实例本地不存仓库
ATLAS_TF_GITEE_TOKEN=…                  # 需要建仓库、读写内容的权限
ATLAS_TF_GITEE_NAMESPACE=               # 空 = token 本人；填组织名 = 建在组织下
ATLAS_TF_REDIS_URL=redis://localhost:6380/0
ATLAS_TF_EMBEDDED_WORKER=true           # API 内是否也跑 Agent 监督器（多实例下开着也安全）

make tf-serve                           # API，可起多个（不同端口 / 副本），前面挂负载均衡
make tf-worker                          # 独立 worker，可起多个
```

| 组件 | 多实例怎么协调 |
| --- | --- |
| 流程状态推进 | 数据库流程行锁（`SELECT … FOR UPDATE`） |
| Agent 投递 / 跟随 | 唤醒经 Redis 广播；**节点租约锁**（`SET NX PX`，持有者每 1/3 租期续租）保证同一节点只有一个实例在处理；进程死掉后租约过期，其他实例经定期扫描（`ATLAS_TF_WORKER_SWEEP_SECONDS`）接手。租约不是工作时长上限 |
| 实时推送 | 事件通知与 Agent 逐段输出经 Redis 发布 / 订阅，每个 API 实例推给自己的 SSE 连接 |
| 团队级别缓存 | 本实例立即失效 + Redis 广播给其他实例 |
| 产物 | Gitee：每个项目一个私有仓库 `{命名空间}/teamflow-{项目 uuid}`，一次提交 = 一个 commit；token 走请求头 |

不配 `ATLAS_TF_REDIS_URL` 时退回单进程内存实现；`ATLAS_TF_GIT_BACKEND=local` 时用本机 git 仓库（只适合单实例）。关闭内嵌监督器却不配 Redis、或选 Gitee 却不给 token 时拒绝启动。

测试：`uv run pytest teamflow/tests`（独立测试库 `atlas_teamflow_test`；用户中心与 Atlas 用内存实现 `tests/tf_fakes.py`；Gitee 用模拟传输；多实例用例用本机 Redis 与随机频道前缀，Redis 不可用时跳过）。★ conftest 会覆盖 `.env` 里的 Gitee / Redis 配置，测试不碰真实外部服务。

## 结构

```
teamflow/
  src/atlas_teamflow/
    db/            表模型（自增 id + uuid 逻辑主键，关联用 uuid）
    migrations/    alembic
    catalog.py     在用户中心登记的操作、菜单、角色、数据编码
    register.py    register-uc：按 catalog 幂等同步到用户中心
    uc.py          用户中心开放接口客户端（唯一接触点）
    atlas.py       Atlas 客户端（Agent 列表 / 详情）
    identity.py    登录（用户中心代为校验密码）、会话、授权快照、团队级别缓存
    templates/     文件模板；流程模板（structure.py 是纯函数：规范化、依赖推导、发布校验）
    process/       流程运行：状态机与锁定（service.py）、Git 产物仓库（git_store.py）、实时广播（bus.py）
    agents/        Agent 适配层：监督器投递指令、跟随 Atlas 事件流、提取 <artifact> 产物、重启续接
    teams/         团队、成员（用户中心 Team 数据权限）、团队 Agent；项目与角色分配
    api/           /api/v1
  tests/
  web/             前端（Next.js + antd）
```

边界：`atlas_teamflow` 与 atlas 其它包（含 `atlas_usercenter`）互不 import（根 pyproject 的 import-linter 契约）。

## 权限的落点

允许 = 角色的操作码 ∧ 团队级别 ∧ 流程角色（节点动作：执行人按项目当前分配，评审人按本轮锁定名单）。

| 动作 | 检查 |
| --- | --- |
| 模板维护 | `flow_template:manage` / `file_template:manage` |
| 新建团队 | `team:create`；一次写入管理员 = Owner、初始成员 = 读写（数据第一次授权，不带 operator） |
| 编辑团队、接入 Agent、新建 / 配置 / 归档项目 | 对应操作码 ∧ 团队 Owner |
| 管理成员 / 设团队管理员 | `team:member` ∧ Owner，或 `team:manage_all`（用户中心「数据管理员」） |
| 查看团队与项目 | 团队读写以上，或 `team:manage_all` |

成员相关写操作都以当前用户为 `operator` 调用户中心，Owner 规则与「至少保留一个 Owner」由用户中心兜底。

## 与设计文档的偏差（首版）

| 项 | 设计 | 实现 | 原因 |
| --- | --- | --- | --- |
| 登录 | SSO（以后） | 用户中心开放接口代为校验密码，TeamFlow 自建会话 | 本期没有 SSO；以后换 Keycloak 只改 `identity.login()` |
| 团队级别缓存失效 | 订阅用户中心 Webhook | 进程内 30 秒缓存，本进程写授权后立即失效 | Webhook 是用户中心 P1 |
| Agent 可用性 | 定时同步（`agent_sync_seconds`） | 团队页「同步可用性」按钮触发 | 定时任务随流程模块一起做 |
| 查看团队 / 项目 | `process:view` ∧ 团队读写 | 只看团队读写 | 团队管理员、成员角色都带 `process:view`，首版不重复检查 |
| 移除成员的阻塞项 | 进行中流程的评审名单 / 唯一执行人 | 不检查，直接连带移除其角色分配 | 待补 |
| 内置流程模板 | `builtin` 标记 | 有字段，无种子 | 平台管理员自建 |
| Worker + Redis 任务队列 | Redis 队列（如 arq） | 不用队列：指令在数据库排队，唤醒经 Redis 广播 + 节点租约锁 + 定期扫描 | 数据库已是排队的唯一真相，少一层状态 |
| Git 服务 | GitLab / Gitea | Gitee API（contents 接口逐文件提交） | 按要求使用 gitee.com |
| 产物链接 | — | 指向 Gitee 私有仓库，需要有仓库权限才能打开 | 页面内预览不依赖 Gitee 权限 |
| 待办索引 | 专门的索引表 | 按条件查询（JSONB 包含） | 数据量下足够 |
| 产物渲染 | 白名单 Markdown 渲染 | 纯文本显示 | 安全优先，P2 换渲染库 |

## Agent 交付产物的约定

开工上下文要求 Agent 把产物全文放在 `<artifact>…</artifact>` 之间（每次输出完整全文）。适配层识别最后一个块并提交为新版本；同一轮里内容相同不重复提交。识别不到时人可以在页面「手工提交产物」。

Atlas 侧用固定的服务身份（`ATLAS_TF_ATLAS_USER_ID`，一个 UUID）建会话；TeamFlow 建的会话标题以 `[TeamFlow]` 开头。
