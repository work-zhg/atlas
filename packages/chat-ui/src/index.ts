/**
 * @atlas/chat-ui —— Atlas 对话组件。
 *
 * 消费方：
 *   · Atlas 前端（web/）：全站已是 Atlas 主题，直接用 ThreadConversation；
 *   · AI TeamFlow 前端（teamflow/web/）：用 AtlasChatTheme 包住对话区，直连 Atlas；只有开新一轮经
 *     TeamFlow（submitRun），能不能操作由它按流程状态给 readOnly。
 *
 * 用法：
 *   <AtlasClientProvider config={{ apiBase, headers }}>
 *     <ThreadConversation threadId={id} />
 *   </AtlasClientProvider>
 *
 * 样式：引入 "@atlas/chat-ui/src/theme/tokens.css"（设计变量）。
 */
export { AtlasClientProvider, useAtlasClient, useAtlasHttp } from "./client";
export { ThreadConversation, type ConversationStreamState, type ThreadConversationProps } from "./chat/ThreadConversation";
export { AtlasChatTheme } from "./chat/AtlasChatTheme";
export { ApiError, type AtlasClientConfig } from "./lib/http";
export type { SendMessageInput, SubmitRun } from "./api/runs";
export type { Message, RunAccepted } from "./api/types";
export { Markdown } from "./components/Markdown";
export { atlasTheme, palette } from "./theme/antd";
// 浏览器（c4-model/agent/browser/design-browser.html）：会话里的实时画面、我的登录
export { BrowserView } from "./chat/BrowserView";
export {
  useBrowserLogins,
  useBrowserStatus,
  useForgetLogin,
  type BrowserLogin,
} from "./api/browser";
