# 用户中心（atlas-usercenter）

用户、组织与权限的唯一数据源。接入的内部系统（AI TeamFlow、Atlas 等）在**服务端**用 App Key / Secret 调用开放接口 `/open/v1` 获取用户、部门与本应用的授权。统一登录（SSO）本期不做，以后用 Keycloak。

设计文档：`prototype/usercenter/docs/`（总体设计 `system-design.html` 与四个模块的详细设计）。

## 运行

```bash
make up                       # PG / Redis 等（会建 atlas_usercenter 库）
make uc-migrate               # 迁移 + 同步内置应用（用户中心自身的操作 / 菜单 / 内置角色）
make uc-bootstrap COMPANY=星海科技 ACCOUNT=admin NAME=管理员 EMAIL=admin@example.com
                              # 首次部署：根部门 + 首个超级管理员（临时密码只输出这一次）
make uc-web-install           # 前端依赖
make uc-dev                   # 后端 :8030 + 前端 :3100（/api 由 Next 反向代理到后端）
```

测试：`uv run pytest usercenter/tests`（独立测试库 `atlas_usercenter_test`，不碰开发库）。

## 结构

```
usercenter/
  src/atlas_usercenter/
    db/            表模型（自增 id + uuid 逻辑主键，外键引用 uuid）
    migrations/    alembic
    builtin.py     内置应用：用户中心自身的操作、菜单、内置角色（每次 migrate 幂等同步）
    org/           组织：部门树、负责人约束与自动清空、直属上级计算
    user/          用户：账号、状态、临时密码、个人中心
    auth/          管理台登录与会话（只服务于用户中心自身）
    app/           应用接入：应用、凭据、可访问范围、操作、菜单、角色、数据编码、接口令牌
    perm/          权限：有效权限计算、超级管理员保护、角色授权、岗位、数据授权
    api/           管理 API（/api/v1）与开放接口（/open/v1）
  tests/           按模块的 HTTP 级测试
  web/             管理台前端（Next.js + antd）
```

边界：`atlas_usercenter` 不 import atlas 的任何包，atlas 的任何包也不 import 它（根 pyproject 的 import-linter 契约）。

## 与设计文档的偏差（首版）

| 项 | 设计 | 实现 | 原因 |
| --- | --- | --- | --- |
| 会话、接口令牌 | Redis | PG（`uc_session`、`uc_app_token`，只存 SHA-256） | 少一个依赖；规模足够 |
| 有效权限缓存 | 按「用户 + 授权版本号」缓存 | 不缓存，每次按索引计算 | 数万用户规模查询足够快；计算入口只有 `perm/effective.py`，需要时再加 |
| 选人 / 选部门 | — | 新增 `/api/v1/directory/users`、`/directory/depts`，登录即可用 | 数据授权的 Owner（普通用户）也要能选人、选组织 |
| 外键 | — | 建外键（引用 uuid） | 本仓运行时库的约定是不建外键；用户中心按其详细设计建外键，库独立，不影响运行时 |

未实现（P1 及以后）：Webhook 变更通知、Secret 平滑轮换、元数据上报、operator 之外的应用内分享界面、安全策略与审计日志的页面、Keycloak。
