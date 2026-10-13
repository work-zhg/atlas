"use client";

/**
 * 记忆（prototype/atlas-v2.html「记忆」页；记忆设计 §10）。
 *
 * ★ 自动抽取意味着用户从没主动说过「请记住这个」，系统却替他记下了 —— 所以每条都
 *   要能回到出处（来源会话）、能删掉。记忆按用户隔离，不按智能体；「智能体」一列
 *   是从来源会话推出来的，便于按场景筛。
 */
import { Alert, App as AntdApp, Button, Empty, Input, Popconfirm, Select, Skeleton, Table } from "antd";
import Link from "next/link";
import { useMemo, useState } from "react";

import { useDeleteMemory, useMemories, useSourceThreads } from "@/api/memories";
import type { Memory, Thread } from "@/api/types";
import { Icon } from "@/components/Icon";
import { relativeTime } from "@/lib/format";
import { ApiError } from "@/lib/http";
import pageStyles from "@/features/agents/agents.module.css";
import { avatarBackground, avatarLetter } from "@/features/agents/avatar";

export function MemoryPage() {
  const { message } = AntdApp.useApp();
  const memoriesQ = useMemories();
  const remove = useDeleteMemory();
  const [keyword, setKeyword] = useState("");
  const [agentId, setAgentId] = useState<string>();

  const memories = memoriesQ.data?.data ?? [];
  const sessionIds = useMemo(
    () => [...new Set(memories.map((m) => m.source_session).filter((s): s is string => !!s))],
    [memories],
  );
  const sourceQs = useSourceThreads(sessionIds);
  const threads = useMemo(() => {
    const map = new Map<string, Thread | null>();
    sessionIds.forEach((id, i) => {
      const q = sourceQs[i];
      if (q?.data !== undefined) map.set(id, q.data);
    });
    return map;
  }, [sessionIds, sourceQs]);

  const agents = useMemo(() => {
    const seen = new Map<string, string>();
    for (const t of threads.values()) if (t) seen.set(t.agent_id, t.agent_name);
    return [...seen.entries()].map(([value, label]) => ({ value, label }));
  }, [threads]);

  const visible = memories.filter((m) => {
    const t = m.source_session ? threads.get(m.source_session) : undefined;
    if (agentId && t?.agent_id !== agentId) return false;
    const kw = keyword.trim().toLowerCase();
    return !kw || m.memory.toLowerCase().includes(kw);
  });

  const disabled = memoriesQ.error instanceof ApiError && memoriesQ.error.status === 503;

  return (
    <div className={pageStyles.page}>
      <header className={pageStyles.head}>
        <div className={pageStyles.titleRow}>
          <div>
            <h1 className={pageStyles.title}>记忆</h1>
            <p className={pageStyles.desc}>
              智能体在对话中替你记下的长期信息。会影响之后的回答 —— 记错了就删掉。
            </p>
          </div>
        </div>
        <div className={pageStyles.filterRow}>
          <Input
            allowClear
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder="搜索内容…"
            prefix={<Icon name="search" size={14} />}
            style={{ width: 260 }}
          />
          <Select
            allowClear
            placeholder="全部智能体"
            style={{ width: 200 }}
            value={agentId}
            onChange={setAgentId}
            options={agents}
          />
          <span style={{ fontSize: 12, color: "var(--fg-3)" }}>
            {memoriesQ.data ? `共 ${memories.length} 条` : ""}
          </span>
        </div>
      </header>

      <div className={pageStyles.body}>
        {disabled && (
          <Alert
            type="info"
            showIcon
            message="记忆功能没有开启"
            description="服务端 MEMORY_ENABLED=false：不会抽取，也没有可看的记忆。"
          />
        )}
        {memoriesQ.error && !disabled && (
          <Alert type="error" showIcon message="加载失败" description={memoriesQ.error.message} />
        )}
        {memoriesQ.isPending && <Skeleton active paragraph={{ rows: 6 }} />}
        {memoriesQ.data && visible.length === 0 && (
          <Empty
            style={{ marginTop: 64 }}
            description={memories.length ? "没有匹配的记忆" : "还没有记下任何东西"}
          />
        )}
        {visible.length > 0 && (
          <Table<Memory>
            rowKey="id"
            size="middle"
            dataSource={visible}
            pagination={visible.length > 50 ? { pageSize: 50 } : false}
            columns={[
              {
                title: "内容",
                render: (_, m) => <span style={{ lineHeight: 1.6 }}>{m.memory}</span>,
              },
              {
                title: "智能体",
                width: 160,
                render: (_, m) => {
                  const t = m.source_session ? threads.get(m.source_session) : undefined;
                  if (!t) return <span style={{ color: "var(--fg-3)" }}>—</span>;
                  return (
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                      <span
                        aria-hidden="true"
                        style={{
                          width: 18,
                          height: 18,
                          borderRadius: 5,
                          display: "grid",
                          placeItems: "center",
                          fontSize: 10,
                          fontWeight: 700,
                          color: "#fff",
                          background: avatarBackground(t.agent_avatar_key),
                        }}
                      >
                        {avatarLetter(t.agent_name)}
                      </span>
                      {t.agent_name}
                    </span>
                  );
                },
              },
              {
                title: "来源会话",
                width: 220,
                render: (_, m) => {
                  if (!m.source_session) return <span style={{ color: "var(--fg-3)" }}>—</span>;
                  const t = threads.get(m.source_session);
                  if (t === null) return <span style={{ color: "var(--fg-3)" }}>会话已删除</span>;
                  // ★ 子智能体的子会话不在会话列表里：链到父会话（委派发生的地方）
                  if (t?.parent_thread_id) {
                    return (
                      <Link href={`/chat/${t.parent_thread_id}`}>
                        {t.subagent_name ?? "子智能体"} 的子会话
                      </Link>
                    );
                  }
                  return (
                    <Link href={`/chat/${m.source_session}`}>{t?.title || "（未命名会话）"}</Link>
                  );
                },
              },
              {
                title: "记录于",
                width: 110,
                render: (_, m) => (
                  <span style={{ color: "var(--fg-2)", fontSize: 12 }}>
                    {relativeTime(m.updated_at ?? m.created_at)}
                  </span>
                ),
              },
              {
                title: "",
                width: 56,
                render: (_, m) => (
                  <Popconfirm
                    title="删除这条记忆？"
                    description="之后的回答不会再用到它。"
                    okText="删除"
                    okButtonProps={{ danger: true }}
                    cancelText="取消"
                    onConfirm={() =>
                      remove
                        .mutateAsync(m.id)
                        .then(() => message.success("已删除"))
                        .catch((e: Error) => message.error(e.message))
                    }
                  >
                    <Button
                      type="text"
                      size="small"
                      danger
                      aria-label="删除这条记忆"
                      icon={<Icon name="trash" size={14} />}
                    />
                  </Popconfirm>
                ),
              },
            ]}
          />
        )}
      </div>
    </div>
  );
}
