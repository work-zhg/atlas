"use client";

/**
 * 主模型的选择：输入框底栏里的一个胶囊，点开是这个智能体能用的模型（按它的 Key 列出）。
 * 选的就是智能体配置的模型 = 跟随智能体。首页与对话页共用。
 *
 * ★ 只换主智能体的模型：SubAgent 用自己配置的。CLI 智能体的模型在拉起 CLI 时就定了，
 *   只显示、不能选（locked 给出原因）。
 */
import { Dropdown } from "antd";

import type { ModelInfo } from "../api/types";
import { Icon } from "../components/Icon";
import styles from "./chat.module.css";

export function ModelPicker({
  value,
  defaultModel,
  models,
  onChange,
  disabled = false,
  locked,
}: {
  value: string;
  /** 智能体配置的主模型 */
  defaultModel: string;
  /** 能选的模型（selectableModels） */
  models: ModelInfo[];
  onChange: (model: string) => void;
  disabled?: boolean;
  /** 不能在对话里换时的原因（CLI 智能体） */
  locked?: string;
}) {
  const label = models.find((m) => m.model === value)?.display_name || value;
  if (locked || models.length === 0) {
    return (
      <span className={styles.pill} style={{ cursor: "default" }} title={locked ?? "模型"}>
        <Icon name="brain" size={14} />
        {label}
      </span>
    );
  }
  return (
    <Dropdown
      trigger={["click"]}
      disabled={disabled}
      menu={{
        selectable: true,
        selectedKeys: [value],
        onClick: ({ key }) => onChange(key),
        items: models.map((m) => ({
          key: m.model,
          label: (
            <div style={{ padding: "2px 0", maxWidth: 280 }}>
              <div style={{ fontWeight: 600 }}>{m.display_name || m.model}</div>
              {m.model === defaultModel && (
                <div style={{ fontSize: 12, color: "var(--fg-3)" }}>智能体默认</div>
              )}
            </div>
          ),
        })),
      }}
    >
      <button type="button" className={styles.pill} disabled={disabled} title="选择模型" aria-label="模型">
        <Icon name="brain" size={14} />
        {label}
        <Icon name="down" size={13} />
      </button>
    </Dropdown>
  );
}
