"use client";

/**
 * 智能体卡片：谁的、做什么、我最近用没用过；主操作是「对话」，能改的才有「编辑」。
 *
 * ★ 不显示标识、版本号、工具个数这类实现信息 —— 用户挑智能体看的是它能帮自己做什么。
 */
import { App as AntdApp, Button, Dropdown, Tag, Tooltip } from "antd";
import { useRouter } from "next/navigation";

import { useCreateAgent, useSetAgentStatus, useSetDefaultAgent } from "@/api/agents";
import type { Agent } from "@/api/types";
import { Icon } from "@/components/Icon";
import { http } from "@/lib/http";
import { relativeTime } from "@/lib/format";
import type { AgentDetail } from "@/api/types";

import styles from "./agents.module.css";
import { avatarBackground, avatarLetter } from "./avatar";
import { newSlug } from "./templates";

/** 能不能改它：以使用者身份的要「编辑」，以它自己身份的要「所有者」 */
export function canEditAgent(a: Pick<Agent, "my_level" | "auth_subject">): boolean {
  return a.my_level === "OWNER" || (a.my_level === "WRITE" && a.auth_subject === "user");
}

export function AgentCard({
  agent,
  isDefault,
  isAdmin,
  archivedView,
  onProfile,
}: {
  agent: Agent;
  isDefault: boolean;
  isAdmin: boolean;
  archivedView?: boolean;
  /** 只能使用的：点开是简介 */
  onProfile: (a: Agent) => void;
}) {
  const router = useRouter();
  const { message, modal } = AntdApp.useApp();
  const setStatus = useSetAgentStatus(agent.id);
  const setDefault = useSetDefaultAgent();
  const createAgent = useCreateAgent();
  const editable = canEditAgent(agent);
  const owner = agent.my_level === "OWNER";
  const who = agent.mine ? "我创建的" : (agent.owner_name ?? "共享给我的");

  const duplicate = async () => {
    try {
      const src = await http.get<AgentDetail>(`/v1/agents/${agent.id}`);
      const copy = await createAgent.mutateAsync({
        slug: newSlug("agent"),
        name: `${agent.name} 副本`,
        description: agent.description,
        avatar_key: agent.avatar_key,
        spec: src.spec as never,
        auth_subject: "user",
      });
      message.success("已复制为新的智能体（草稿）");
      router.push(`/agents/${copy.id}`);
    } catch (e) {
      message.error((e as Error).message);
    }
  };

  const archive = () =>
    modal.confirm({
      title: `归档「${agent.name}」？`,
      content: "归档后不能再发起新对话，已有的对话仍然保留。可以随时在「已归档」里恢复。",
      okText: "归档",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: () => setStatus.mutateAsync("archived").then(() => message.success("已归档")),
    });

  const menu = [
    { key: "dup", icon: <Icon name="copy" size={14} />, label: "复制为新智能体" },
    ...(isAdmin && agent.status === "enabled" && !isDefault
      ? [{ key: "default", icon: <Icon name="star" size={14} />, label: "设为默认智能体" }]
      : []),
    ...(owner ? [{ type: "divider" as const }, { key: "archive", icon: <Icon name="archive" size={14} />, label: "归档", danger: true }] : []),
  ];

  return (
    <div
      className={styles.card}
      role="button"
      tabIndex={0}
      onClick={() => (archivedView ? undefined : editable ? router.push(`/agents/${agent.id}`) : onProfile(agent))}
      onKeyDown={(e) => e.key === "Enter" && !archivedView && (editable ? router.push(`/agents/${agent.id}`) : onProfile(agent))}
    >
      <div className={styles.cardHead}>
        <span className={styles.avatar} style={{ background: avatarBackground(agent.avatar_key) }} aria-hidden="true">
          {avatarLetter(agent.name)}
        </span>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div className={styles.cardName}>
            {agent.name}
            {isDefault && <Tag color="processing" style={{ marginInlineEnd: 0 }}>默认</Tag>}
            {agent.status === "draft" && <Tag color="warning" style={{ marginInlineEnd: 0 }}>草稿</Tag>}
            {agent.kind === "cli" && <Tag color="blue" style={{ marginInlineEnd: 0 }}>Claude Code</Tag>}
          </div>
          <div className={styles.cardSub}>{who}</div>
        </div>
      </div>
      <div className={styles.cardDesc}>{agent.description || "（没有描述）"}</div>
      <div className={styles.cardFoot} onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
        {archivedView ? (
          <>
            <span>归档于 {relativeTime(agent.updated_at)}</span>
            <span style={{ flex: 1 }} />
            {owner && (
              <Button
                size="small"
                loading={setStatus.isPending}
                onClick={() => setStatus.mutate("enabled", { onSuccess: () => message.success("已恢复") })}
              >
                恢复
              </Button>
            )}
          </>
        ) : (
          <>
            <Icon name="clock" size={13} />
            <span>{agent.last_used_at ? `最近使用 ${relativeTime(agent.last_used_at)}` : "还没用过"}</span>
            <span style={{ flex: 1 }} />
            {editable && (
              <Button size="small" type="text" onClick={() => router.push(`/agents/${agent.id}`)}>
                编辑
              </Button>
            )}
            <Tooltip title={agent.status === "draft" ? "发布后才能对话" : undefined}>
              <Button
                size="small"
                type="primary"
                icon={<Icon name="chat" size={13} />}
                disabled={agent.status !== "enabled"}
                onClick={() => router.push(`/?agent=${agent.id}`)}
              >
                对话
              </Button>
            </Tooltip>
            <Dropdown
              trigger={["click"]}
              placement="bottomRight"
              menu={{
                items: menu,
                onClick: ({ key }) => {
                  if (key === "dup") void duplicate();
                  if (key === "default")
                    setDefault.mutate(agent.id, {
                      onSuccess: () => message.success("已设为默认智能体"),
                      onError: (e) => message.error(e.message),
                    });
                  if (key === "archive") archive();
                },
              }}
            >
              <Button size="small" type="text" icon={<Icon name="more" size={15} />} aria-label="更多" />
            </Dropdown>
          </>
        )}
      </div>
    </div>
  );
}
