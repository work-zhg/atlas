/**
 * @atlas/chat-ui —— Atlas 对话组件。
 *
 * 消费方：
 *   · Atlas 前端（web/）：全站已是 Atlas 主题，直接用 ThreadConversation；
 *   · AI TeamFlow 前端（teamflow/web/）：用 AtlasChatTheme 包住对话区，接口经其按节点限定的代理。
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
export { atlasTheme, palette } from "./theme/antd";
