# @atlas/chat-ui

Atlas 的对话组件 —— **Atlas 前端（`web/`）与 AI TeamFlow 前端（`teamflow/web/`）共用同一份实现**。
对话的任何升级（新事件类型、卡片、交互）改这里，两边重新构建即生效，不维护第二套。

## 内容

| 目录 | 说明 |
| --- | --- |
| `chat/ThreadConversation.tsx` | 一条会话的完整对话区：头部、消息流（实时轨迹 / 审批 / 子智能体 / 待办 / 压缩）、输入框 |
| `chat/*` | 消息流与各类卡片、会话事件流 hook、样式 |
| `lib/` | 事件契约、事件归约、会话 SSE、请求核心（`ApiError` / 错误信封） |
| `api/` | 会话 / 运行的读写 hooks、接口类型（`schema.d.ts` 由 Atlas 前端 `pnpm gen:api` 生成） |
| `theme/` | 设计变量 `tokens.css` 与 antd 主题 —— 设计系统唯一来源 |
| `client.tsx` | 注入点 `AtlasClientProvider`：接口根地址 + 请求头（组件不读环境变量） |

## 用法

```tsx
import { AtlasChatTheme, AtlasClientProvider, ThreadConversation } from "@atlas/chat-ui";
import "@atlas/chat-ui/src/theme/tokens.css";

<AtlasClientProvider config={{ apiBase, headers }}>
  <AtlasChatTheme style={{ height: 640 }}>   {/* 嵌入方用；Atlas 全站已是该主题 */}
    <ThreadConversation threadId={id} readOnly={!canSend} />
  </AtlasChatTheme>
</AtlasClientProvider>
```

- Atlas：`apiBase` = Atlas 后端（`web/src/lib/http.ts` 的 `atlasClient`）。
- TeamFlow：`apiBase` = 节点的 Atlas 会话代理 `/api/v1/processes/{流程}/nodes/{节点}/atlas`，
  权限（读：团队成员；写：执行人）在 TeamFlow 后端守住（`teamflow/src/atlas_teamflow/api/atlas_proxy.py`）。

## 约定

- 以源码形式被消费：消费方 `next.config` 的 `transpilePackages` 含 `@atlas/chat-ui`。
- 依赖在前端 workspace（仓库根 `pnpm-workspace.yaml`）里安装，React / antd / react-query 与消费方是同一实例（peerDependencies）。
- 组件里调用的接口路径与 Atlas 后端完全一致；新增调用的接口时，TeamFlow 代理的放行清单（`atlas_proxy.py`）要同步。
- 改完跑：`pnpm --filter atlas-web check`、`pnpm --filter atlas-teamflow-web typecheck`、`cd packages/chat-ui && pnpm typecheck`。
