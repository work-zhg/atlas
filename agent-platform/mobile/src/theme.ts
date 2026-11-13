/**
 * 设计 token：与网页原型同一套浅色 token（agent-platform/doc/prototype/mobile.html）。
 * 字号、间距按原型的手机界面取；颜色只从这里拿，不在组件里写色值。
 */
export const C = {
  bg: "#f6f7f9",
  surface: "#ffffff",
  surface2: "#fbfbfd",
  hover: "#f2f4f8",
  ink: "#0f172a",
  ink2: "#334155",
  ink3: "#64748b",
  ink4: "#94a3b8",
  line: "#e6e8ee",
  line2: "#eef0f4",
  primary: "#4f46e5",
  primary2: "#6366f1",
  primaryWeak: "#eef0ff",
  primaryInk: "#4338ca",
  secondaryWeak: "#e6f6fe",
  secondaryInk: "#0369a1",
  success: "#16a34a",
  successWeak: "#e7f6ec",
  warning: "#d97706",
  warningWeak: "#fef3e2",
  warningInk: "#92400e",
  warnCardBg: "#fffcf5",
  warnCardLine: "#f5d9a8",
  danger: "#dc2626",
  dangerWeak: "#fdeaea",
  dangerInk: "#991b1b",
  bubble: "#f1f3f9",
  code: "#0f172a",
  codeHead: "#1e293b",
  codeInk: "#e2e8f0",
  mask: "rgba(15,23,42,0.32)",
} as const;

export const R = { xs: 6, sm: 8, md: 12, lg: 16, xl: 20, pill: 999 } as const;

/** 字号：正文 15，次要 13，辅助 12；标题 16/17；输入框 16（iOS 聚焦时不放大页面） */
export const F = { xs: 12, sm: 13, md: 14, body: 15, input: 16, title: 16, h1: 26 } as const;

export const MONO = "monospace";

/** 头像配色（同 chat-ui 的 avatar_key 取值；RN 不支持渐变，取渐变的起始色） */
const AVATAR: Record<string, string> = {
  analyst: "#0EA5E9",
  review: "#8B5CF6",
  research: "#F59E0B",
  sre: "#EF4444",
  doc: "#10B981",
  general: "#6366F1",
  coder: "#38BDF8",
  support: "#34D399",
  writer: "#FBBF24",
};
export function avatarColor(key: string | null | undefined): string {
  return AVATAR[key ?? ""] ?? AVATAR.general!;
}
