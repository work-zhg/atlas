import type { Level, Usage } from "./types";

export const LEVEL: Record<Level, { label: string; color: string }> = {
  OWNER: { label: "团队管理员", color: "purple" },
  WRITE: { label: "成员", color: "blue" },
  READ: { label: "只读", color: "default" },
  NONE: { label: "无", color: "default" },
};

export const USAGE: Record<Usage, string> = { artifact: "产物格式", review_rule: "评审规则", other: "其他" };

export function dt(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function size(bytes: number): string {
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`;
}

export const RULE = { any: "任一人同意", all: "所有人同意" } as const;
