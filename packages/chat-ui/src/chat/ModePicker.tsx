"use client";

/**
 * 执行前确认（审批模式）的选择：输入框底栏里的一个胶囊，点开是三个选项各带一句说明。
 * 首页与对话页共用（doc/native-approval-mode-design.html §11）。
 */
import { Dropdown } from "antd";

import { Icon } from "../components/Icon";
import type { ApprovalMode } from "../lib/events";
import styles from "./chat.module.css";

export const MODE_TEXT: Record<ApprovalMode, { label: string; hint: string }> = {
  auto: { label: "自动", hint: "常规操作直接执行，有风险的先问你" },
  manual: { label: "手动", hint: "每次动手前都先问你" },
  plan: { label: "先规划", hint: "先给出计划，你确认后再执行" },
};

const ORDER: ApprovalMode[] = ["auto", "manual", "plan"];

export function ModePicker({
  value,
  allowed,
  onChange,
  disabled = false,
}: {
  value: ApprovalMode;
  allowed: ApprovalMode[];
  onChange: (m: ApprovalMode) => void;
  disabled?: boolean;
}) {
  const options = ORDER.filter((m) => allowed.includes(m));
  if (options.length === 0) return null;
  const current = MODE_TEXT[value] ?? MODE_TEXT.auto;
  return (
    <Dropdown
      trigger={["click"]}
      disabled={disabled}
      menu={{
        selectable: true,
        selectedKeys: [value],
        onClick: ({ key }) => onChange(key as ApprovalMode),
        items: options.map((m) => ({
          key: m,
          label: (
            <div style={{ padding: "2px 0", maxWidth: 260 }}>
              <div style={{ fontWeight: 600 }}>{MODE_TEXT[m].label}</div>
              <div style={{ fontSize: 12, color: "var(--fg-3)" }}>{MODE_TEXT[m].hint}</div>
            </div>
          ),
        })),
      }}
    >
      <button type="button" className={styles.pill} disabled={disabled} title={current.hint} aria-label="执行前确认">
        <Icon name="shield" size={14} />
        {current.label}
        <Icon name="down" size={13} />
      </button>
    </Dropdown>
  );
}
