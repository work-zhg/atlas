/**
 * 技能应用（atlas-skill）的领域类型 —— 来自 skill-schema.d.ts。
 *
 * ★ 同样不要手写字段。技能应用改了 schema 就重跑 `pnpm gen:api:skill`。
 */
import type { components } from "./skill-schema";

type C = components["schemas"];

export type SkillFile = C["SkillFile"];

/**
 * ★ 带默认值的列表字段在生成类型里是可选的（default_factory ⇒ 不在 required 里），
 *   但响应里必然存在。这里收窄，免得每个读取点都写 `?? []`（同 ResolvedSpec 的做法）。
 */
export type ManagedSkillVersion = C["SkillVersionAdminOut"] & {
  files: SkillFile[];
  scan_result: Record<string, unknown>;
};
export type ManagedSkill = Omit<C["SkillDetailOut"], "versions"> & {
  versions: ManagedSkillVersion[];
};
export type AuditEntry = C["AuditOut"];

/** 当前用户在技能应用里能做什么（GET /api/v1/me；只是体验，权限在各接口上） */
export interface SkMe {
  uid: string;
  can_upload: boolean;
  can_review: boolean;
  can_audit: boolean;
}
