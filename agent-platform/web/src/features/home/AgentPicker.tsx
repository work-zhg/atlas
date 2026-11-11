"use client";

/** 首页输入框里选智能体：默认的排第一，最后是「浏览全部智能体」。 */
import { Dropdown } from "antd";
import { useRouter } from "next/navigation";

import type { Agent } from "@/api/types";
import { Icon } from "@/components/Icon";
import { avatarBackground, avatarLetter } from "@/features/agents/avatar";
import chat from "@atlas/chat-ui/src/chat/chat.module.css";

function Avatar({ agent, size = 18 }: { agent: Pick<Agent, "name" | "avatar_key">; size?: number }) {
  return (
    <span
      aria-hidden="true"
      style={{
        width: size,
        height: size,
        borderRadius: 5,
        background: avatarBackground(agent.avatar_key),
        color: "#fff",
        fontSize: Math.round(size * 0.5),
        fontWeight: 700,
        display: "inline-grid",
        placeItems: "center",
        flex: "none",
      }}
    >
      {avatarLetter(agent.name)}
    </span>
  );
}

export function AgentPicker({
  agents,
  value,
  defaultId,
  onChange,
}: {
  agents: Agent[];
  value?: Agent;
  defaultId?: string;
  onChange: (id: string) => void;
}) {
  const router = useRouter();
  const sorted = [...agents].sort((a, b) => Number(b.id === defaultId) - Number(a.id === defaultId));
  return (
    <Dropdown
      trigger={["click"]}
      menu={{
        selectable: true,
        selectedKeys: value ? [value.id] : [],
        items: [
          { key: "_title", type: "group", label: "选择智能体" },
          ...sorted.slice(0, 8).map((a) => ({
            key: a.id,
            label: (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                <Avatar agent={a} size={20} />
                {a.name}
                {a.id === defaultId && (
                  <span style={{ fontSize: 11, color: "var(--primary-text)", background: "var(--primary-soft)", borderRadius: 4, padding: "0 5px" }}>
                    默认
                  </span>
                )}
              </span>
            ),
          })),
          { type: "divider" },
          { key: "_all", icon: <Icon name="grid" size={14} />, label: "浏览全部智能体" },
        ],
        onClick: ({ key }) => (key === "_all" ? router.push("/agents") : onChange(key)),
      }}
    >
      <button type="button" className={chat.pill} aria-label="选择智能体">
        {value ? <Avatar agent={value} /> : <Icon name="bot" size={14} />}
        {value?.name ?? "选择智能体"}
        <Icon name="down" size={13} />
      </button>
    </Dropdown>
  );
}
