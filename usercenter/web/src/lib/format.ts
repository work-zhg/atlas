import type { Level, UserStatus } from "./types";

export const STATUS: Record<UserStatus, { label: string; color: string }> = {
  active: { label: "正常", color: "green" },
  pending: { label: "未激活", color: "blue" },
  locked: { label: "已锁定", color: "red" },
  disabled: { label: "已停用", color: "default" },
};

export const LEVEL: Record<Level, { label: string; color: string }> = {
  READ: { label: "只读", color: "default" },
  WRITE: { label: "读写", color: "blue" },
  OWNER: { label: "Owner", color: "purple" },
  NONE: { label: "无权限", color: "default" },
};

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 应用入口地址：同源绝对路径（不是 // 开头的协议相对地址）或 http(s) 完整地址 —— 与后端 HOME_URL_RE 一致 */
export const HOME_URL_RULE = { pattern: /^(\/(?![/\\])\S*|https?:\/\/\S+)$/, message: "以 / 开头的站内路径，或 http(s):// 开头的完整地址" };
