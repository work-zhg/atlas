"use client";

import { Tag } from "antd";
import { useRouter } from "next/navigation";

import type { Agent } from "@/api/types";
import { Icon } from "@/components/Icon";
import { relativeTime } from "@/lib/format";
import styles from "./agents.module.css";
import { avatarBackground, avatarLetter } from "./avatar";
import { AGENT_STATUS_META } from "./status";

export function AgentCard({ agent }: { agent: Agent }) {
  const router = useRouter();
  const status = AGENT_STATUS_META[agent.status];

  return (
    <div
      className={styles.card}
      role="button"
      tabIndex={0}
      onClick={() => router.push(`/agents/${agent.id}`)}
      onKeyDown={(e) => {
        // 键盘可达：div 当按钮用就必须自己处理 Enter/Space
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          router.push(`/agents/${agent.id}`);
        }
      }}
    >
      <div className={styles.cardHead}>
        <span
          className={styles.avatar}
          style={{ background: avatarBackground(agent.avatar_key) }}
          aria-hidden="true"
        >
          {avatarLetter(agent.name)}
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className={styles.cardName}>
            {agent.name}
            <Tag color={status.color} style={{ marginInlineEnd: 0 }}>
              {status.label}
            </Tag>
            {agent.is_builtin && <Tag style={{ marginInlineEnd: 0 }}>内置</Tag>}
          </div>
          <div className={styles.cardSlug}>{agent.slug}</div>
        </div>
      </div>

      <p className={styles.cardDesc}>{agent.description || "暂无描述"}</p>

      <div className={styles.cardTags}>
        <Tag className={styles.mono} style={{ marginInlineEnd: 0 }}>
          {agent.model}
        </Tag>
        <Tag style={{ marginInlineEnd: 0 }}>v{agent.version}</Tag>
        {agent.subagent_count > 0 && (
          <Tag color="processing" style={{ marginInlineEnd: 0 }}>
            {agent.subagent_count} 子智能体
          </Tag>
        )}
      </div>

      <div className={styles.cardFoot}>
        <span>
          <Icon name="wrench" size={14} />
          {agent.tool_count} 工具
        </span>
        <span className="last">{relativeTime(agent.updated_at)}</span>
      </div>
    </div>
  );
}
