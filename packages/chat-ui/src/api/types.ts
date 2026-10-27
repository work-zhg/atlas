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
/** 权限类型（数字员工设计 §7.1）：user = 以人为权限，agent = 以 Agent 为权限 */
export type AuthSubject = Agent["auth_subject"];
export type AgentIdentity = S["AgentIdentityOut"];

export type Thread = S["ThreadOut"];
export type ThreadCreate = S["ThreadCreate"];
export type Message = S["MessageOut"];

export type Run = S["RunOut"];
export type RunAccepted = S["RunAccepted"];
export type PendingApprovalList = S["PendingApprovalList"];
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

// ---------- 技能 / MCP（运行时侧的只读视图，doc/skill-mcp-backend-design.html §13.3）

export type SkillListOut = S["SkillListOut"];
export type SkillListItem = S["SkillListItem"];
export type SkillReference = S["SkillReference"];
export type SkillUsageOut = S["SkillUsageOut"];
export type McpServerListOut = S["McpServerListOut"];
export type McpServerSummary = S["McpServerSummary"];
export type McpServerDetail = S["McpServerDetailOut"];
export type McpTool = S["McpToolOut"];
export type SkillRefIn = S["SkillRefIn"];

// ---------- 记忆（doc/memory-design.html §7：管理接口 /v1/memories）

export type MemoryTopic = S["TopicOut"];
export type MemoryTopicDetail = S["TopicDetailOut"];
export type MemoryEntry = S["EntryOut"];
export type MemoryEntryDetail = S["EntryDetailOut"];
export type MemoryEntryList = S["EntryListOut"];
export type MemoryWrite = S["WriteOut"];
export type MemoryRevision = S["RevisionOut"];
export type MemorySettings = S["MemorySettingsOut"];
export type MemoryBackfill = S["BackfillOut"];
export type WorkspaceFile = S["WorkspaceFile"];
export type WorkspaceFilesOut = S["WorkspaceFilesOut"];
export type PreviewSessionOut = S["PreviewSessionOut"];
