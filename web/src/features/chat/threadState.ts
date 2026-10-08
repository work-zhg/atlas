import type { Thread } from "@/api/types";

export interface ThreadBadge {
  label: string;
  /** antd Tag 的 color */
  color: string;
  /** 需要用户处理（审批）—— 「需要我处理」筛选用 */
  needsMe: boolean;
  /** 这一轮还没结束（运行中 / 挂起）—— 「进行中」筛选用 */
  active: boolean;
  /** 正在跑：徽标带呼吸点 */
  live: boolean;
}

/**
 * 会话最近一个 run 的状态 → 列表徽标（prototype/atlas-v2.html 会话列表）。
 *
 * ★ 审批优先于一切：委派出去的子智能体在等审批时，父 run 显示的是「等待子
 *   智能体」，但真正卡住的是用户 —— 后端已把它算进 needs_approval。
 * ★ 正常结束 / 取消的不显示徽标：列表里大多数会话都是这样，满屏「已完成」只是噪音。
 */
export function threadBadge(thread: Pick<Thread, "last_run">): ThreadBadge | null {
  const run = thread.last_run;
  if (!run) return null;
  if (run.needs_approval) {
    return { label: "待审批", color: "warning", needsMe: true, active: true, live: false };
  }
  switch (run.status) {
    case "queued":
    case "running":
      return { label: "运行中", color: "success", needsMe: false, active: true, live: true };
    case "suspended":
      return {
        label: run.waiting.includes("delegation") ? "等待子智能体" : "已挂起",
        color: "processing",
        needsMe: false,
        active: true,
        live: true,
      };
    case "awaiting_approval":
      return { label: "待审批", color: "warning", needsMe: true, active: true, live: false };
    case "failed":
    case "interrupted":
      return { label: "失败", color: "error", needsMe: false, active: false, live: false };
    default:
      return null;
  }
}
