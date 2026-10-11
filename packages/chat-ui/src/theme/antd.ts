/**
 * AntD 主题 —— 照抄 design-system/atlas/MASTER.md §5。
 *
 * ★ 这些颜色的对比度是实测过的（MASTER.md §2 有比值表），不要凭感觉调：
 *   DB 原推荐的绿色 #22C55E 作 Primary 时白字仅 1.8:1，严重不合格，
 *   换成 indigo #4F46E5 才达到 6.29:1。改色前先算对比度。
 */
import { theme, type ThemeConfig } from "antd";

/** 语义色 —— 与 globals.css 的 CSS 变量保持一致，两边都要改。 */
export const palette = {
  bgBase: "#0F172A",
  bgContainer: "#111B31",
  bgElevated: "#16223C",
  bgRail: "#0B1220",
  borderSplit: "#22304D",
  borderControl: "#3A4C72",
  text: "#F8FAFC",
  textSecondary: "#94A3B8",
  /** ⚠️ 3.75:1 —— 仅 placeholder / disabled，禁止用于正文 */
  textTertiary: "#64748B",
  primary: "#4F46E5",
  /** 深底上的链接/焦点色，5.98:1 */
  primaryText: "#818CF8",
  success: "#22C55E",
  warning: "#F59E0B",
  error: "#EF4444",
} as const;

/** MASTER.md §6：不走 Google Fonts CDN（大陆不可达且 FOIT），用系统字体栈。 */
export const FONT_STACK =
  'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif';

export const atlasTheme: ThemeConfig = {
  algorithm: theme.darkAlgorithm,
  token: {
    colorPrimary: palette.primary,
    colorInfo: palette.primary,
    colorSuccess: palette.success,
    colorWarning: palette.warning,
    colorError: palette.error,
    colorBgBase: palette.bgBase,
    colorBgContainer: palette.bgContainer,
    colorBgElevated: palette.bgElevated,
    colorBorder: palette.borderControl,
    colorBorderSecondary: palette.borderSplit,
    colorText: palette.text,
    colorTextSecondary: palette.textSecondary,
    colorTextTertiary: palette.textTertiary,
    borderRadius: 8,
    controlHeight: 32,
    fontSize: 14,
    fontFamily: FONT_STACK,
    motionDurationMid: "0.18s",
  },
  components: {
    Layout: { siderBg: palette.bgRail, headerBg: palette.bgBase },
    Menu: {
      darkItemBg: "transparent",
      darkItemSelectedBg: "rgba(99,102,241,0.14)",
    },
  },
};
