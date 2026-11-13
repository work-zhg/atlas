/**
 * 一个对话里各处卡片共用的东西：打开文件预览、打开子智能体的过程页、步骤详情。
 * 由对话页（Conversation）提供。
 */
import { createContext, useContext } from "react";

import type { RunState, SubagentRun, ToolCall } from "@atlas/chat-ui/src/lib/run-reducer";

/** 在主轮次里找某个委派（主 run 的，或子 run 再委派的） */
export function findSubagent(root: RunState, runId: string): SubagentRun | undefined {
  return (
    root.subagents.find((x) => x.runId === runId) ??
    Object.values(root.subRuns)
      .flatMap((r) => r.subagents)
      .find((x) => x.runId === runId)
  );
}

export interface ChatCtxValue {
  threadId: string;
  /**
   * 工具参数里的文件路径 → 这个对话工作目录里的相对路径；不在工作目录里（或还没出现在文件列表里）为 null。
   * ★ 按文件列表核对，不按路径的样子猜：对得上才给「预览」。
   */
  resolveFile: (toolPath: string) => string | null;
  openFile: (path: string) => void;
  openStep: (call: ToolCall) => void;
  /** subId = 子 run 的 run_id（过程在 root.subRuns 里）；root = 卡片所在的主轮次状态（实时区或某一轮的轨迹） */
  openSubRun: (run: SubagentRun, subId: string | undefined, root: RunState) => void;
}

export const ChatCtx = createContext<ChatCtxValue | null>(null);

export function useChat(): ChatCtxValue {
  const c = useContext(ChatCtx);
  if (!c) throw new Error("缺少 ChatCtx");
  return c;
}
