"use client";

/**
 * 智能体列表：只按「和我的关系」筛（全部 / 我创建的 / 共享给我的），默认按最近使用排序、默认智能体置顶。
 *
 * ★ 已归档的不混在列表里，从「已归档」进；类型不做筛选维度（Claude Code 驱动的只在卡片上打个标签）。
 * ★ 创建是弹窗：名称 + 从哪个模板开始，建好直接进编辑页。
 */
import { App as AntdApp, Button, Empty, Input, Modal, Segmented, Select, Skeleton } from "antd";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";

import { useAgents, useCreateAgent, useDefaultAgent } from "@/api/agents";
import { useModels } from "@/api/meta";
import type { Agent } from "@/api/types";
import { Icon } from "@/components/Icon";
import { AgentCard } from "@/features/agents/AgentCard";
import styles from "@/features/agents/agents.module.css";
import { avatarBackground, avatarLetter } from "@/features/agents/avatar";
import { createBody, TEMPLATES, type TemplateId } from "@/features/agents/templates";
import { can, useMe } from "@/lib/session";

type Scope = "all" | "mine" | "shared";
type Sort = "recent" | "updated" | "name";

const time = (s?: string | null) => (s ? new Date(s).getTime() : 0);

function CreateModal({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const { message } = AntdApp.useApp();
  const createAgent = useCreateAgent();
  const modelsQ = useModels();
  const [name, setName] = useState("");
  const [tpl, setTpl] = useState<TemplateId>("general");
  const model = (modelsQ.data?.data ?? []).find((m) => m.is_available && m.selectable)?.model ?? "claude-sonnet-5";
  const picked = TEMPLATES.find((t) => t.id === tpl)!;

  return (
    <Modal
      open
      title="创建智能体"
      okText="创建"
      cancelText="取消"
      width={620}
      confirmLoading={createAgent.isPending}
      onCancel={onClose}
      onOk={() =>
        createAgent.mutate(createBody(picked, name.trim() || picked.name, model), {
          onSuccess: (a) => router.push(`/agents/${a.id}`),
          onError: (e) => message.error(e.message),
        })
      }
    >
      <div style={{ fontWeight: 600, marginBottom: 6 }}>名称</div>
      <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="例如：合同审阅助手" maxLength={64} autoFocus />
      <div style={{ fontWeight: 600, margin: "16px 0 6px" }}>从哪里开始</div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
        {TEMPLATES.map((t) => (
          <div
            key={t.id}
            role="radio"
            aria-checked={tpl === t.id}
            tabIndex={0}
            className={styles.template}
            onClick={() => setTpl(t.id)}
            onKeyDown={(e) => e.key === "Enter" && setTpl(t.id)}
          >
            <span
              className={styles.avatar}
              style={{ width: 32, height: 32, fontSize: 13, background: t.id === "blank" ? "var(--line-2)" : avatarBackground(t.avatarKey) }}
            >
              {t.id === "blank" ? <Icon name="plus" size={15} /> : avatarLetter(t.name)}
            </span>
            <div>
              <b>{t.name}</b>
              <div style={{ fontSize: 12.5, color: "var(--fg-3)" }}>{t.description}</div>
            </div>
          </div>
        ))}
      </div>
    </Modal>
  );
}

function ProfileModal({ agent, onClose }: { agent: Agent; onClose: () => void }) {
  const router = useRouter();
  return (
    <Modal open footer={null} onCancel={onClose} width={460}>
      <div style={{ textAlign: "center", padding: "8px 8px 4px" }}>
        <span
          className={styles.avatar}
          style={{ width: 64, height: 64, fontSize: 26, borderRadius: 16, margin: "0 auto", background: avatarBackground(agent.avatar_key) }}
        >
          {avatarLetter(agent.name)}
        </span>
        <h2 style={{ margin: "12px 0 2px", fontSize: 18 }}>{agent.name}</h2>
        <div style={{ fontSize: 13, color: "var(--fg-3)" }}>{agent.owner_name ? `${agent.owner_name} 创建` : "共享给我的"}</div>
        <p style={{ margin: "14px 0 18px", color: "var(--fg-2)" }}>{agent.description || "（没有描述）"}</p>
        <Button
          type="primary"
          icon={<Icon name="chat" size={14} />}
          disabled={agent.status !== "enabled"}
          onClick={() => router.push(`/?agent=${agent.id}`)}
        >
          开始对话
        </Button>
      </div>
    </Modal>
  );
}

function AgentsPage() {
  const router = useRouter();
  const params = useSearchParams();
  const { data: me } = useMe();
  const archivedView = params.get("view") === "archived";
  const [scope, setScope] = useState<Scope>("all");
  const [keyword, setKeyword] = useState("");
  const [sort, setSort] = useState<Sort>("recent");
  const [creating, setCreating] = useState(params.get("create") === "1");
  const [profile, setProfile] = useState<Agent>();

  const listQ = useAgents({ scope: scope === "all" ? undefined : scope });
  const archivedQ = useAgents({ status: "archived" });
  const defaultQ = useDefaultAgent();
  const defaultId = defaultQ.data?.id;

  const agents = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    const rows = (archivedView ? archivedQ.data?.data : listQ.data?.data)?.filter(
      (a) => (archivedView ? a.status === "archived" : a.status !== "archived"),
    );
    const found = (rows ?? []).filter((a) => !kw || `${a.name} ${a.description}`.toLowerCase().includes(kw));
    const by: Record<Sort, (a: Agent, b: Agent) => number> = {
      recent: (a, b) => time(b.last_used_at) - time(a.last_used_at) || time(b.updated_at) - time(a.updated_at),
      updated: (a, b) => time(b.updated_at) - time(a.updated_at),
      name: (a, b) => a.name.localeCompare(b.name, "zh"),
    };
    const sorted = [...found].sort(by[sort]);
    // 默认智能体置顶
    return [...sorted.filter((a) => a.id === defaultId), ...sorted.filter((a) => a.id !== defaultId)];
  }, [listQ.data, archivedQ.data, archivedView, keyword, sort, defaultId]);

  const archivedCount = (archivedQ.data?.data ?? []).filter((a) => a.status === "archived").length;
  const loading = archivedView ? archivedQ.isPending : listQ.isPending;

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <div className={styles.titleRow}>
          {archivedView && (
            <Button type="text" icon={<Icon name="left" size={16} />} onClick={() => router.push("/agents")} aria-label="返回" />
          )}
          <h1 className={styles.title}>{archivedView ? "已归档的智能体" : "智能体"}</h1>
          {!archivedView && can(me, "agent:create") && (
            <Button type="primary" className={styles.action} icon={<Icon name="plus" size={14} />} onClick={() => setCreating(true)}>
              创建智能体
            </Button>
          )}
        </div>
        {!archivedView && (
          <div className={styles.toolbar}>
            <Segmented
              value={scope}
              onChange={(v) => setScope(v as Scope)}
              options={[
                { value: "all", label: "全部" },
                { value: "mine", label: "我创建的" },
                { value: "shared", label: "共享给我的" },
              ]}
            />
            <Input
              allowClear
              style={{ width: 260 }}
              prefix={<Icon name="search" size={14} style={{ color: "var(--fg-4)" }} />}
              placeholder="搜索智能体"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
            />
            <span style={{ flex: 1 }} />
            <Select
              style={{ width: 120 }}
              value={sort}
              onChange={setSort}
              options={[
                { value: "recent", label: "最近使用" },
                { value: "updated", label: "最近更新" },
                { value: "name", label: "名称" },
              ]}
            />
            {archivedCount > 0 && (
              <Button type="text" icon={<Icon name="archive" size={14} />} onClick={() => router.push("/agents?view=archived")}>
                已归档 {archivedCount}
              </Button>
            )}
          </div>
        )}
      </header>

      <div className={styles.body}>
        {loading ? (
          <Skeleton active paragraph={{ rows: 6 }} />
        ) : agents.length === 0 ? (
          <Empty
            style={{ marginTop: 60 }}
            description={keyword ? "没有找到，换个关键词试试" : archivedView ? "没有已归档的智能体" : "还没有智能体"}
          >
            {!keyword && !archivedView && can(me, "agent:create") && (
              <Button type="primary" onClick={() => setCreating(true)}>
                创建智能体
              </Button>
            )}
          </Empty>
        ) : (
          <div className={styles.grid}>
            {agents.map((a) => (
              <AgentCard
                key={a.id}
                agent={a}
                isDefault={a.id === defaultId}
                isAdmin={can(me, "config:manage")}
                archivedView={archivedView}
                onProfile={setProfile}
              />
            ))}
          </div>
        )}
      </div>

      {creating && <CreateModal onClose={() => setCreating(false)} />}
      {profile && <ProfileModal agent={profile} onClose={() => setProfile(undefined)} />}
    </div>
  );
}

export default function Page() {
  return (
    <Suspense>
      <AgentsPage />
    </Suspense>
  );
}
