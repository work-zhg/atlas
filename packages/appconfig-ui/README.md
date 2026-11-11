# @atlas/appconfig-ui

应用配置库 `atlas_appconfig`（`infra-platform/libs/appconfig`）的管理界面 —— **任何挂了 `ConfigRoutes` 的应用直接嵌入**，
不用各写一套。原型：`c4-model/infra-platform/ui-appconfig.html`。

## 内容

| 文件 | 说明 |
| --- | --- |
| `AppConfigPanel.tsx` | 面板：按前缀分组的列表、过滤（全部 / 已修改 / 默认值）、搜索；打开编辑、历史抽屉 |
| `EditDrawer.tsx` | 编辑抽屉：按类型编辑、修改前后对比、修改说明、恢复默认、版本冲突处理 |
| `HistoryDrawer.tsx` | 修改历史与回滚（确认弹窗里显示当前 → 回滚后的差异） |
| `ValueEditor.tsx` | 按类型的编辑器与草稿（整数、小数、开关、文本、列表、映射；列表和映射可切 JSON） |
| `ValueView.tsx` | 值的展示与差异 |
| `value.ts` | 类型解析、校验（与 `types.py` 一致）、差异算法 |
| `api.ts` | 五个管理接口的类型、请求与 hooks（与 `routes.py` 一一对应） |

## 用法

```tsx
import { AppConfigPanel } from "@atlas/appconfig-ui";

<AppConfigPanel
  endpoint={`${API_BASE}/v1/configs`}  // 宿主挂 ConfigRoutes 的位置
  headers={authHeaders}                 // 可选：每次请求附带的头（如 CSRF）
  readOnly={!canEdit}                   // 可选：只能看（对应 ConfigGuard.edit 不通过）
  renderUser={(by) => nameOf(by)}       // 可选：修订历史里的操作人标识 → 姓名
/>
```

- 宿主要有 antd 5 与 @tanstack/react-query 5（`QueryClientProvider`）；提示与确认弹窗用组件自己的 holder，不要求宿主套 `<App>`。
- 样式只用 antd 主题变量，跟着宿主的主题走；页面标题、导航由宿主画。
- Atlas：`agent-platform/web/src/app/configs/page.tsx`（看和改都要 `config:manage`）。

## 约定

- 以源码形式被消费：消费方 `next.config` 的 `transpilePackages` 含 `@atlas/appconfig-ui`；前端镜像
  （`docker/frontends/Dockerfile`）安装依赖前要拷它的 `package.json`。
- 接口类型是手写的（库不属于任何宿主，没有可生成类型的 openapi.json）：改 `routes.py` 的
  `ConfigOut` / `RevisionOut` / `SetIn` / `RollbackIn` 时同步改 `api.ts`；新增值类型时同步改 `value.ts`。
- 比库更严的两条前端校验：映射的键不能为空；列表逐项编辑时不能有空项（白名单里空前缀等于全放行，
  确实要空字符串就切到 JSON 编辑）。
- 改完跑：`cd packages/appconfig-ui && pnpm typecheck`、`pnpm --filter atlas-web check`。
