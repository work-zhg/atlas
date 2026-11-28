"use client";

/**
 * 对话是不是只读：只读时没有输入框，卡片（审批、提问、接管）也只显示、不能操作。
 *
 * ★ 用上下文而不是逐层传 props：卡片在消息流、实时轨迹、子智能体卡片里都会出现，层级很深。
 *   由 ThreadConversation 按它的 readOnly 提供。
 */
import { createContext, useContext } from "react";

export const ConversationReadOnly = createContext(false);

export function useConversationReadOnly(): boolean {
  return useContext(ConversationReadOnly);
}
