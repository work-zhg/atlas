"use client";

import { Tag } from "antd";
import { useRouter } from "next/navigation";

import type { Agent } from "@/api/types";
import { Icon } from "@/components/Icon";
import { relativeTime } from "@/lib/format";
import styles from "./agents.module.css";
import { avatarBackground, avatarLetter } from "./avatar";
import { AGENT_STATUS_META } from "./status";

const MODE_SHORT: Record<string, string> = {
  auto: "Auto",
  manual: "手动",
  accept_edits: "自动编辑",
  plan: "仅规划",
};

/** 卡片上直接看出能力面（prototype/atlas-v2.html 智能体列表 ⑥）。 */
export function AgentCard({ agent }: { agent: Agent }) {
  const router = useRouter();
  const status = AGENT_STATUS_META[agent.status];
  const isAcp = agent.kind === "acp";

  return (
    <div
      className={`${styles.card} ${agent.status === "archived" ? styles.cardMuted : ""}`}
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
            {agent.is_builtin && <Tag style={{ marginInlineEnd: 0 }}>内置</Tag>}
          </div>
          <div className={styles.cardSlug}>
            {agent.slug} · v{agent.version}
          </div>
        </div>
        <Tag color={status.color} style={{ marginInlineEnd: 0 }}>
          {status.label}
        </Tag>
      </div>

      <p className={styles.cardDesc}>{agent.description || "暂无描述"}</p>

      <div className={styles.cardTags}>
        {isAcp ? (
          <Tag color="orange" style={{ marginInlineEnd: 0 }}>
            ACP · CLI
          </Tag>
        ) : (
          <Tag color="purple" style={{ marginInlineEnd: 0 }}>
            Native
          </Tag>
        )}
        <Tag className={styles.mono} style={{ marginInlineEnd: 0 }}>
          {agent.model}
        </Tag>
        {agent.builtin_tool_count > 0 && (
          <Tag style={{ marginInlineEnd: 0 }}>工具 {agent.builtin_tool_count}</Tag>
        )}
        {agent.skill_count > 0 && <Tag style={{ marginInlineEnd: 0 }}>技能 {agent.skill_count}</Tag>}
        {agent.mcp_servers.length > 0 && (
          <Tag color="cyan" style={{ marginInlineEnd: 0 }} title={agent.mcp_servers.join("、")}>
            MCP {agent.mcp_servers.length}
          </Tag>
        )}
        {/* 委派关系：卡片上直接看出它会把活交给谁、以什么权限模式 */}
        {agent.subagents.map((s) => (
          <Tag
            key={String(s.name)}
            color={s.kind === "acp" ? "orange" : "processing"}
            style={{ marginInlineEnd: 0 }}
          >
            → {String(s.name)}
            {s.kind === "acp" && ` · ${MODE_SHORT[String(s.permission_mode ?? "auto")] ?? s.permission_mode}`}
          </Tag>
        ))}
      </div>

      <div className={styles.cardFoot}>
        <span>
          <Icon name="clock" size={13} />
          更新于 {relativeTime(agent.updated_at)}
        </span>
      </div>
    </div>
  );
}
