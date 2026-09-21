/**
 * 领域类型别名 —— 全部来自 schema.d.ts（openapi-typescript 从 /openapi.json 生成）。
 *
 * ★ 不要在这里手写字段。后端改了 schema 就重跑 `pnpm gen:api`，
 *   类型不匹配会在 tsc 阶段暴露，而不是等到运行时某个字段是 undefined。
 */
import type { components } from "./schema";

type S = components["schemas"];

export type Agent = S["AgentOut"];
export type AgentDetail = S["AgentDetailOut"];
export type AgentCreate = S["AgentCreate"];
export type AgentUpdate = S["AgentUpdate"];
export type AgentSpecIn = S["AgentSpecIn"];
export type AgentVersion = S["AgentVersionOut"];
export type AgentStatus = Agent["status"];

export type Thread = S["ThreadOut"];
export type ThreadCreate = S["ThreadCreate"];
export type Message = S["MessageOut"];

export type Run = S["RunOut"];
export type RunAccepted = S["RunAccepted"];
export type RunStatus = Run["status"];

export type ModelInfo = S["ModelInfo"];
export type ToolInfo = S["ToolInfo"];

export type ModelSpecIn = S["ModelSpecIn"];
export type LimitSpecIn = S["LimitSpecIn"];
export type CompactionSpecIn = S["CompactionSpecIn"];

/** 游标分页信封（文档 §11：keyset 分页，不用 OFFSET） */
export interface Page<T> {
  data: T[];
  next_cursor?: string | null;
}

/** run 是否已进入终态 —— SSE 要不要继续连、能不能取消，都看它 */
export const TERMINAL_RUN_STATUSES = [
  "succeeded",
  "failed",
  "cancelled",
  "interrupted",
] as const satisfies readonly RunStatus[];

export function isTerminal(status: RunStatus): boolean {
  return (TERMINAL_RUN_STATUSES as readonly string[]).includes(status);
}
