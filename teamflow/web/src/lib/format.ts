import type { Level, MarketTemplate, NodeStatus, ProcessStatus, Rule } from "./types";

export const LEVEL: Record<Level, { label: string; color: string }> = {
  OWNER: { label: "团队管理员", color: "purple" },
  WRITE: { label: "读写", color: "blue" },
  READ: { label: "只读", color: "default" },
  NONE: { label: "无", color: "default" },
};

const RANK: Record<Level, number> = { NONE: 0, READ: 1, WRITE: 2, OWNER: 3 };
export const atLeast = (lv: Level | null | undefined, min: Level) => RANK[lv ?? "NONE"] >= RANK[min];

/**
 * 步骤（后端叫节点）的七种状态：文字 + 颜色（流程图、标签、进度条共用）。
 * hint 是悬停说明：状态名说不全的那半句。
 */
export const NODE_STATUS: Record<NodeStatus, { label: string; color: string; bg: string; hint?: string }> = {
  pending: { label: "未开始", color: "#64748b", bg: "#f1f5f9" },
  working: { label: "进行中", color: "#1d4ed8", bg: "#e0ecff" },
  exit_review: { label: "准出评审中", color: "#b45309", bg: "#fef3e2" },
  exited: { label: "已准出", color: "#0f766e", bg: "#ddf7f2", hint: "等下一步的人确认接收" },
  admit_review: { label: "准入评审中", color: "#7e22ce", bg: "#f5edff", hint: "下一步的人在审上一步交付的文件" },
  passed: { label: "已完成", color: "#15803d", bg: "#e3f7e8" },
  returned: { label: "被退回", color: "#b91c1c", bg: "#fdeaea", hint: "上一步重做后再走准入" },
};

/** 成员上传资料的默认文件夹（数字员工开工时会收到上传过的文件清单） */
export const MATERIALS_DIR = "资料";

export const PROCESS_STATUS: Record<ProcessStatus, { label: string; color: string }> = {
  running: { label: "进行中", color: "processing" },
  completed: { label: "已完成", color: "success" },
  terminated: { label: "已终止", color: "default" },
};

export const MARKET_STATUS: Record<MarketTemplate["status"], [string, string]> = {
  listed: ["上架中", "success"],
  unlisted: ["已下架", "default"],
  draft: ["还没发布", "warning"],
};

export const RULE: Record<Rule, string> = { any: "任一人同意", all: "全部同意" };
export const STAGE = { admit: "准入", exit: "准出" } as const;

export function dt(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 相对时间：刚刚、5 分钟前、3 小时前，再早就是日期 */
export function ago(iso: string | null | undefined): string {
  if (!iso) return "—";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  if (s < 86400 * 2) return "昨天";
  return dt(iso);
}

export function size(bytes: number | null | undefined): string {
  if (bytes == null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** 路径的最后一段（文件名） */
export const base = (p: string) => p.replace(/\/$/, "").split("/").pop() ?? p;
