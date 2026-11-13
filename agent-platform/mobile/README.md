# Atlas 手机 App（Android 先行）

只做三样：**对话、工作空间、文件预览**。智能体管理、知识库、技能、MCP、模型、个性化、系统都留在电脑版。
设计稿：`agent-platform/doc/prototype/mobile.html`（用浏览器打开，右侧有每屏的设计说明）。

技术栈：React Native + Expo（SDK 58）+ Expo Router。iOS 以后用同一份代码。

## 和网页共用什么

| 共用 | 位置 |
|---|---|
| 接口 hooks（会话、消息、发送、审批、取消、轨迹、反馈、模型） | `packages/chat-ui/src/api` |
| 事件流（SSE）与事件归约（run-reducer） | `packages/chat-ui/src/lib` |
| 对话怎么摆（哪几条消息要画、实时区让不让位、步骤怎么分组） | `packages/chat-ui/src/lib/conversation.ts` |
| 文件用哪种方式预览 | `packages/chat-ui/src/lib/previewKind.ts` |

界面组件是 App 自己的（`src/`），网页的 antd 组件在手机上用不了。

接法：`package.json` 里 `"@atlas/chat-ui": "link:../../packages/chat-ui"`，`metro.config.js` 把 chat-ui 加进 watchFolders，
并让 chat-ui 里的第三方 import 一律从 App 自己的 `node_modules` 取（否则会有两个 React）。

## 登录

和网页同一套：调用户中心 `POST /uc/api/v1/auth/login`，会话在 Cookie 里（系统的 CookieManager 管，之后的请求自动带上）；
写请求带 `X-UC-CSRF`，值是登录时 `Set-Cookie` 里的 `uc_csrf`，存在 SecureStore。后端不用为 App 改东西。
服务器地址在登录页可以改，默认 `https://atlas.techstz.com`。只支持 HTTPS（`app.json` 里关了明文 HTTP），没写协议时补 `https://`。

## 目录

```
src/app/            路由（Expo Router）：(main) 抽屉里的新对话 / 工作空间 / 对话，preview、folder、search 推进来
src/features/chat/  对话：时间线、步骤、审批 / 提问 / 子智能体卡片、输入框、选择器
src/features/files/ 文件面板、预览、分享 / 用其他应用打开 / 保存
src/features/workspace/  工作空间、搜索
src/features/home/  新对话（首页）
src/features/shell/ 侧边菜单、登录
src/lib/session.tsx 服务器地址、登录会话、CSRF
src/api/queries.ts  App 用到的其余接口（列表、新建、文件、工作空间、设备）
```

## 开发

```bash
# 依赖：独立安装（不进根目录的前端 workspace），pnpm 11（nvm 的 node 22 下有）
pnpm install

npx expo run:android        # 构建并装到模拟器 / 手机（第一次要下载 Gradle 依赖，走 ~/.gradle 里配的代理）
npx expo start              # 只改 JS 时：起 Metro，App 里热更新
pnpm typecheck
```

- `android/` 是 `npx expo prebuild` 生成的，不入库、不手改；原生配置写在 `app.json`（config plugins）。
- 加带原生代码的库用 `npx expo install <包名>`（会挑和 SDK 匹配的版本），加完要重新 `expo run:android`。
- npmmirror 对刚发布的包经常 504：装新包时可以临时 `--registry=https://registry.npmjs.org` 并设 `HTTPS_PROXY`。

## 已知限制

- 用户中心的会话 12 小时过期，手机上过期后要重新登录。
- 没有推送：一轮停下来等你确认时收不到通知（待定）。
