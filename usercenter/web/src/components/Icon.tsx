/**
 * 图标 —— Lucide 风格描边图标，与 Atlas 同一套（照抄 packages/chat-ui/src/components/Icon.tsx 的画法，
 * 只留用户中心用到的，另补了几个：组织、楼宇、岗位、工牌、数据库、应用切换等）。
 *
 * 不用 emoji 当图标：跨平台字形不一致，屏幕阅读器会读成表情名。应用自带的 icon 字段是数据，照常显示。
 */
import type { CSSProperties } from "react";

const PATHS = {
  plus: "M12 5v14M5 12h14",
  search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14m9.5 16L16.5 16.5",
  right: "m9 18 6-6-6-6",
  down: "m6 9 6 6 6-6",
  left: "m15 18-6-6 6-6",
  up: "M12 19V5M5 12l7-7 7 7",
  arrowRight: "M5 12h14M13 6l6 6-6 6",
  copy: "M11 9h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-8a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1",
  more: "M5 11a1 1 0 1 0 0 2 1 1 0 0 0 0-2M12 11a1 1 0 1 0 0 2 1 1 0 0 0 0-2M19 11a1 1 0 1 0 0 2 1 1 0 0 0 0-2",
  waffle: "M5 4a1 1 0 1 0 0 2 1 1 0 0 0 0-2M12 4a1 1 0 1 0 0 2 1 1 0 0 0 0-2M19 4a1 1 0 1 0 0 2 1 1 0 0 0 0-2M5 11a1 1 0 1 0 0 2 1 1 0 0 0 0-2M12 11a1 1 0 1 0 0 2 1 1 0 0 0 0-2M19 11a1 1 0 1 0 0 2 1 1 0 0 0 0-2M5 18a1 1 0 1 0 0 2 1 1 0 0 0 0-2M12 18a1 1 0 1 0 0 2 1 1 0 0 0 0-2M19 18a1 1 0 1 0 0 2 1 1 0 0 0 0-2",
  trash: "M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6",
  x: "M18 6 6 18M6 6l12 12",
  check: "m20 6-11 11-5-5",
  pencil: "M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z",
  refresh: "M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5",
  alert: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18M12 8v4M12 16h.01",
  info: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18M12 16v-4M12 8h.01",
  help: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3M12 17h.01",
  clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18M12 7v5l3 2",
  external: "M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6",
  folder: "M4 20a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2z",
  building: "M6 2h12a2 2 0 0 1 2 2v18H4V4a2 2 0 0 1 2-2M9 22v-4h6v4M8 6h.01M12 6h.01M16 6h.01M8 10h.01M12 10h.01M16 10h.01M8 14h.01M12 14h.01M16 14h.01",
  sitemap: "M9 2h6v5H9zM2 17h6v5H2zM16 17h6v5h-6zM12 7v5M5 17v-3a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v3",
  user: "M12 4a4 4 0 1 0 0 8 4 4 0 0 0 0-8M4 21a8 8 0 0 1 16 0",
  users: "M9 4a4 4 0 1 0 0 8 4 4 0 0 0 0-8M2 21a7 7 0 0 1 14 0M16 3.1a4 4 0 0 1 0 7.8M22 21a7 7 0 0 0-5-6.7",
  userPlus: "M9 4a4 4 0 1 0 0 8 4 4 0 0 0 0-8M2 21a7 7 0 0 1 14 0M19 8v6M16 11h6",
  bot: "M3 11h18v10H3zM12 3a2 2 0 1 1 0 4 2 2 0 0 1 0-4M12 7v4M8 16h.01M16 16h.01",
  shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10",
  badge: "M5 4h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2M9 8.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5M5.5 17a3.5 3.5 0 0 1 7 0M15 10h3M15 14h3",
  briefcase: "M4 7h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16",
  database: "M12 2c5 0 9 1.3 9 3s-4 3-9 3-9-1.3-9-3 4-3 9-3M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5M3 12c0 1.7 4 3 9 3s9-1.3 9-3",
  box: "M21 8 12 3 3 8v8l9 5 9-5zM3 8l9 5 9-5M12 13v8",
  grid: "M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z",
  list: "M9 6h12M9 12h12M9 18h12M4 6h.01M4 12h.01M4 18h.01",
  key: "M7.5 11a4.5 4.5 0 1 0 0 9 4.5 4.5 0 0 0 0-9m3.2 1.3 9.8-9.8M17 6l3 3M14 9l2 2",
  lock: "M6 11h12a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2M8 11V7a4 4 0 0 1 8 0v4",
  unlock: "M6 11h12a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2M8 11V7a4 4 0 0 1 7.7-1.5",
  ban: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18M5.7 5.7l12.6 12.6",
  eye: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6",
  eyeOff: "M9.9 4.2A10 10 0 0 1 12 4c6.5 0 10 8 10 8a17 17 0 0 1-2.2 3.2M6.6 6.6A17 17 0 0 0 2 12s3.5 8 10 8a9.7 9.7 0 0 0 5.4-1.6M14.1 14.1a3 3 0 1 1-4.2-4.2M2 2l20 20",
  login: "M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4M10 17l5-5-5-5M15 12H3",
  logout: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
  star: "m12 3 2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z",
} as const;

export type IconName = keyof typeof PATHS;

export interface IconProps {
  name: IconName;
  size?: number;
  className?: string;
  style?: CSSProperties;
}

export function Icon({ name, size = 16, className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      style={{ flex: "none", ...style }}
      aria-hidden="true"
      focusable="false"
    >
      <path d={PATHS[name]} />
    </svg>
  );
}
