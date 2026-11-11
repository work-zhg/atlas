/**
 * 头像配色 —— 从 prototype 的 .av-* 移植。
 *
 * 后端 avatar_key 是自由字符串（默认 "general"），未知取值回落到 general，
 * 不要因为多了一个 key 就渲染成透明块。
 */
const GRADIENTS: Record<string, string> = {
  analyst: "linear-gradient(135deg,#0EA5E9,#2563EB)",
  review: "linear-gradient(135deg,#8B5CF6,#6366F1)",
  research: "linear-gradient(135deg,#F59E0B,#EA580C)",
  sre: "linear-gradient(135deg,#EF4444,#BE123C)",
  doc: "linear-gradient(135deg,#10B981,#059669)",
  general: "linear-gradient(135deg,#6366F1,#4F46E5)",
  coder: "linear-gradient(135deg,#38BDF8,#0284C7)",
  support: "linear-gradient(135deg,#34D399,#059669)",
  writer: "linear-gradient(135deg,#FBBF24,#D97706)",
};

export const AVATAR_KEYS = Object.keys(GRADIENTS);

export function avatarBackground(key: string): string {
  return GRADIENTS[key] ?? GRADIENTS.general!;
}

/** 取名称首字作为头像文字。中文取首字，英文取首字母大写。 */
export function avatarLetter(name: string): string {
  const ch = name.trim().charAt(0);
  return ch ? ch.toUpperCase() : "?";
}
