/**
 * 配置服务（atlas-config）的领域类型 —— 来自 config-schema.d.ts。
 *
 * ★ 同样不要手写字段。配置服务改了 schema 就重跑 `pnpm gen:api:config`。
 */
import type { components } from "./config-schema";

type C = components["schemas"];

export type SkillFile = C["SkillFile"];

/**
 * ★ 带默认值的列表字段在生成类型里是可选的（default_factory ⇒ 不在 required 里），
 *   但响应里必然存在。这里收窄，免得每个读取点都写 `?? []`（同 ResolvedSpec 的做法）。
 */
export type ConfigSkillVersion = C["SkillVersionAdminOut"] & {
  files: SkillFile[];
  scan_result: Record<string, unknown>;
};
export type ConfigSkill = Omit<C["SkillDetailOut"], "versions"> & {
  versions: ConfigSkillVersion[];
};
export type ConfigMcpServer = C["McpServerOut"];
export type McpServerIn = C["McpServerIn"];
export type McpServerPatch = C["McpServerPatch"];
export type ToolReview = C["ToolReviewOut"];
export type ToolReviewIn = C["ToolReviewIn"];
export type AuditEntry = C["AuditOut"];
