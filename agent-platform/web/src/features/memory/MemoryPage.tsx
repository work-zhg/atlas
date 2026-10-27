"use client";

/**
 * 记忆（doc/memory-design.html §7）：关于当前用户的长期记忆，按主题整理。
 *
 *   左栏  主题目录：常驻 / 按需、当前大小（超过上限的标出来，下次补记时会拆小）
 *   右栏  条目：来源（用户亲自 / Agent 写入 / 会话后补记）、出处会话、演变过程；
 *         更正、移动、彻底删除、恢复已删除的；修订历史里撤销某次写入
 *   顶部  暂停写入（「暂停优于删除」）、添加记忆、删除全部；会话后补记的提示
 *
 * ★ 这些操作只在这里由用户完成，不会作为工具开放给智能体（§7）。影响常驻内容的改动下一轮生效。
 */
import {
  Alert,
  App as AntdApp,
  Button,
  Empty,
  Input,
  Popconfirm,
  Segmented,
  Select,
  Skeleton,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import {
  type EntryQuery,
  SOURCE_LABEL,
  isMemoryDisabled,
  memoryApi,
  useMemoryEntries,
  useMemoryMutation,
  useMemorySettings,
  useMemoryTopics,
  useSourceThreads,
} from "@/api/memories";
import type { MemoryEntry, MemoryTopic, Thread } from "@/api/types";
import { Icon } from "@/components/Icon";
import pageStyles from "@/features/agents/agents.module.css";

import { BackfillNotice } from "./BackfillNotice";
import { AddEntryModal, CorrectEntryModal, MoveEntriesModal, TopicEditModal } from "./dialogs";
import { EntryHistoryDrawer } from "./EntryHistoryDrawer";
import { RevisionsTab } from "./RevisionsTab";
import { localDate } from "./time";

const PAGE_SIZE = 50;
const SOURCE_COLOR: Record<string, string> = { user: "blue", agent: "purple", backfill: "gold" };

export function MemoryPage() {
  const { message } = AntdApp.useApp();
  const topicsQ = useMemoryTopics();
  const settingsQ = useMemorySettings();
  const [topic, setTopic] = useState<string | null>(null);
  const [tab, setTab] = useState("entries");
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<MemoryTopic | null>(null);
  const [history, setHistory] = useState<number | null>(null);
  const setPaused = useMemoryMutation(memoryApi.setPaused);
  const purgeAll = useMemoryMutation(memoryApi.purgeAll);
  const deleteTopic = useMemoryMutation(memoryApi.deleteTopic);

  const disabled = isMemoryDisabled(topicsQ.error);
  const topics = topicsQ.data?.data ?? [];
  const limit = topicsQ.data?.topic_max_chars ?? 0;
  const current = topics.find((t) => t.code === topic) ?? null;

  // 选中的主题被拆分 / 删除了：回到「全部主题」
  useEffect(() => {
    if (topic && topicsQ.data && !current) setTopic(null);
  }, [topic, topicsQ.data, current]);

  const paused = settingsQ.data?.paused ?? false;

  return (
    <div className={pageStyles.page}>
      <header className={pageStyles.head}>
        <div className={pageStyles.titleRow}>
          <div>
            <h1 className={pageStyles.title}>记忆</h1>
            <p className={pageStyles.desc}>
              关于你的长期记忆，按主题整理。常驻主题每次对话都会带上，其余主题由智能体需要时加载。记错了可以更正、撤销或删除。
            </p>
          </div>
          {!disabled && (
            <Space className={pageStyles.action} size={12}>
              <Tooltip title="暂停期间智能体和会话后补记都不再写入；已有的记忆照常使用">
                <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13 }}>
                  <Switch
                    size="small"
                    checked={paused}
                    loading={setPaused.isPending}
                    disabled={!settingsQ.data}
                    onChange={(v) =>
                      setPaused
                        .mutateAsync(v)
                        .then(() => message.success(v ? "已暂停写入" : "已恢复写入"))
                        .catch((e: Error) => message.error(e.message))
                    }
                  />
                  暂停写入
                </span>
              </Tooltip>
              <Button icon={<Icon name="plus" size={14} />} onClick={() => setAdding(true)} disabled={!topicsQ.data}>
                添加记忆
              </Button>
              <Popconfirm
                title="删除全部记忆？"
                description="彻底删除，连同修订历史，不可恢复。"
                okText="全部删除"
                okButtonProps={{ danger: true }}
                cancelText="取消"
                onConfirm={() =>
                  purgeAll
                    .mutateAsync(undefined)
                    .then(() => message.success("已删除全部记忆"))
                    .catch((e: Error) => message.error(e.message))
                }
              >
                <Button danger type="text" disabled={!topics.length}>
                  删除全部
                </Button>
              </Popconfirm>
            </Space>
          )}
        </div>
      </header>

      <div className={pageStyles.body}>
        {disabled && (
          <Alert
            type="info"
            showIcon
            message="记忆功能没有接入"
            description="服务端没有配置记忆服务（MEMORY_BASE_URL）：不会记住任何东西，也没有可看的记忆。"
          />
        )}
        {topicsQ.error && !disabled && (
          <Alert type="error" showIcon message="加载失败" description={topicsQ.error.message} />
        )}
        {topicsQ.isPending && <Skeleton active paragraph={{ rows: 6 }} />}
        {paused && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            message="记忆写入已暂停：智能体和会话后补记都不会写入新的记忆，已有的记忆照常使用。"
          />
        )}
        {topicsQ.data && <BackfillNotice />}
        {topicsQ.data && (
          <div style={{ display: "grid", gridTemplateColumns: "240px minmax(0, 1fr)", gap: 24 }}>
            <TopicList topics={topics} limit={limit} selected={topic} onSelect={setTopic} />
            <div style={{ minWidth: 0 }}>
              {current && (
                <TopicHeader
                  topic={current}
                  limit={limit}
                  onEdit={() => setEditing(current)}
                  onDelete={() =>
                    deleteTopic
                      .mutateAsync(current.code)
                      .then(() => message.success("已删除主题"))
                      .catch((e: Error) => message.error(e.message))
                  }
                />
              )}
              <Tabs
                activeKey={tab}
                onChange={setTab}
                items={[
                  {
                    key: "entries",
                    label: "条目",
                    children: <EntriesPanel topics={topics} topic={topic} onOpenEntry={setHistory} />,
                  },
                  {
                    key: "revisions",
                    label: "修订历史",
                    children: tab === "revisions" ? <RevisionsTab onOpenEntry={setHistory} /> : null,
                  },
                ]}
              />
            </div>
          </div>
        )}
      </div>

      <AddEntryModal open={adding} topics={topics} defaultTopic={topic} onClose={() => setAdding(false)} />
      <TopicEditModal topic={editing} onClose={() => setEditing(null)} />
      <EntryHistoryDrawer number={history} onClose={() => setHistory(null)} />
    </div>
  );
}

function TopicList({
  topics,
  limit,
  selected,
  onSelect,
}: {
  topics: MemoryTopic[];
  limit: number;
  selected: string | null;
  onSelect: (code: string | null) => void;
}) {
  const total = topics.reduce((n, t) => n + t.entry_count, 0);
  const item = (key: string | null, title: React.ReactNode, meta: React.ReactNode) => {
    const active = selected === key;
    return (
      <button
        key={key ?? "__all__"}
        type="button"
        onClick={() => onSelect(key)}
        style={{
          display: "flex",
          flexDirection: "column",
          gap: 2,
          padding: "8px 10px",
          textAlign: "left",
          border: `1px solid ${active ? "var(--line)" : "transparent"}`,
          borderRadius: 8,
          background: active ? "var(--bg-2)" : "transparent",
          color: "inherit",
          cursor: "pointer",
        }}
      >
        <span style={{ fontWeight: 600, fontSize: 13, display: "flex", alignItems: "center", gap: 6 }}>{title}</span>
        <span style={{ fontSize: 12, color: "var(--fg-3)" }}>{meta}</span>
      </button>
    );
  };
  return (
    <nav aria-label="记忆主题" style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      {item(null, "全部主题", `${topics.length} 个主题 · ${total} 条`)}
      {topics.map((t) =>
        item(
          t.code,
          <>
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t.code}</span>
            {t.resident && (
              <Tag color="cyan" style={{ margin: 0 }}>
                常驻
              </Tag>
            )}
          </>,
          <>
            {t.entry_count} 条 · {t.char_count} 字
            {t.over_limit && limit > 0 && <span style={{ color: "var(--warning)" }}> · 超出上限</span>}
          </>,
        ),
      )}
      {!topics.length && (
        <Typography.Text type="secondary" style={{ fontSize: 12, padding: "8px 10px" }}>
          还没有主题：智能体保存第一条记忆时会建好，也可以手动添加。
        </Typography.Text>
      )}
    </nav>
  );
}

function TopicHeader({
  topic,
  limit,
  onEdit,
  onDelete,
}: {
  topic: MemoryTopic;
  limit: number;
  onEdit: () => void;
  onDelete: () => void;
}) {
  return (
    <div style={{ display: "flex", alignItems: "flex-start", gap: 12, marginBottom: 4 }}>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Typography.Title level={5} style={{ margin: 0 }}>
            {topic.code}
          </Typography.Title>
          <Tag color={topic.resident ? "cyan" : "default"}>{topic.resident ? "常驻" : "按需加载"}</Tag>
          {topic.over_limit && (
            <Tooltip title={`超过了每个主题约 ${limit} 字的上限：下次会话后补记时会拆成更细的主题`}>
              <Tag color="warning">超出上限</Tag>
            </Tooltip>
          )}
        </div>
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          {topic.description || "（没有说明）"}
        </Typography.Text>
      </div>
      <Space size={4}>
        <Button size="small" onClick={onEdit}>
          修改主题
        </Button>
        <Popconfirm
          title="删除这个主题？"
          okText="删除"
          okButtonProps={{ danger: true }}
          cancelText="取消"
          disabled={topic.entry_count > 0}
          onConfirm={onDelete}
        >
          <Tooltip title={topic.entry_count > 0 ? "主题里还有条目：先移走或删除" : undefined}>
            <Button size="small" type="text" danger disabled={topic.entry_count > 0}>
              删除
            </Button>
          </Tooltip>
        </Popconfirm>
      </Space>
    </div>
  );
}

function originThread(e: MemoryEntry): string | undefined {
  const thread = e.origin?.thread;
  return typeof thread === "string" ? thread : undefined;
}

function EntriesPanel({
  topics,
  topic,
  onOpenEntry,
}: {
  topics: MemoryTopic[];
  topic: string | null;
  onOpenEntry: (n: number) => void;
}) {
  const { message } = AntdApp.useApp();
  const [status, setStatus] = useState<EntryQuery["status"]>("active");
  const [source, setSource] = useState<string>();
  const [keyword, setKeyword] = useState("");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<number[]>([]);
  const [correcting, setCorrecting] = useState<MemoryEntry | null>(null);
  const [moving, setMoving] = useState<number[] | null>(null);
  const restore = useMemoryMutation((n: number) => memoryApi.restoreEntry(n));
  const purge = useMemoryMutation(memoryApi.purgeEntry);

  useEffect(() => {
    setPage(1);
    setSelected([]);
  }, [topic, status, source, keyword]);

  const entriesQ = useMemoryEntries({
    status,
    topic: topic ?? undefined,
    source,
    q: keyword || undefined,
    limit: PAGE_SIZE,
    offset: (page - 1) * PAGE_SIZE,
  });
  const rows = entriesQ.data?.data ?? [];
  const threadIds = useMemo(
    () => [...new Set(rows.map(originThread).filter((t): t is string => !!t))],
    [rows],
  );
  const threadQs = useSourceThreads(threadIds);
  const threads = useMemo(() => {
    const map = new Map<string, Thread | null>();
    threadIds.forEach((id, i) => {
      const q = threadQs[i];
      if (q?.data !== undefined) map.set(id, q.data);
    });
    return map;
  }, [threadIds, threadQs]);

  function act(p: Promise<unknown>, ok: string) {
    p.then(() => message.success(ok)).catch((e: Error) => message.error(e.message));
  }

  return (
    <>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 12 }}>
        <Segmented
          value={status}
          onChange={(v) => setStatus(v as EntryQuery["status"])}
          options={[
            { value: "active", label: "有效" },
            { value: "deleted", label: "已删除" },
            { value: "all", label: "全部" },
          ]}
        />
        <Select
          allowClear
          placeholder="全部来源"
          style={{ width: 140 }}
          value={source}
          onChange={setSource}
          options={Object.entries(SOURCE_LABEL).map(([value, label]) => ({ value, label }))}
        />
        <Input.Search allowClear placeholder="搜索内容" style={{ width: 220 }} onSearch={setKeyword} />
        {selected.length > 0 && status === "active" && (
          <Button size="small" onClick={() => setMoving(selected)}>
            移动选中的 {selected.length} 条
          </Button>
        )}
        <span style={{ fontSize: 12, color: "var(--fg-3)", marginLeft: "auto" }}>
          {entriesQ.data ? `共 ${entriesQ.data.total} 条` : ""}
        </span>
      </div>
      {entriesQ.error && <Alert type="error" showIcon message={entriesQ.error.message} />}
      {entriesQ.data && rows.length === 0 ? (
        <Empty style={{ marginTop: 48 }} description={status === "deleted" ? "没有已删除的记忆" : "这里还没有记忆"} />
      ) : (
        <Table<MemoryEntry>
          rowKey="number"
          size="middle"
          loading={entriesQ.isFetching && !entriesQ.data}
          dataSource={rows}
          rowSelection={
            status === "active"
              ? { selectedRowKeys: selected, onChange: (keys) => setSelected(keys as number[]) }
              : undefined
          }
          pagination={
            (entriesQ.data?.total ?? 0) > PAGE_SIZE
              ? { current: page, pageSize: PAGE_SIZE, total: entriesQ.data?.total, onChange: setPage }
              : false
          }
          columns={[
            {
              title: "内容",
              render: (_, e) => (
                <span style={{ lineHeight: 1.6, opacity: e.status === "active" ? 1 : 0.65 }}>
                  {e.content}
                  <span style={{ color: "var(--fg-3)", fontFamily: "var(--mono)", fontSize: 12, marginLeft: 6 }}>
                    #{e.number}
                  </span>
                  {e.status !== "active" && (
                    <Tag style={{ marginLeft: 6 }}>{e.superseded_by ? `被 #${e.superseded_by} 取代` : "已遗忘"}</Tag>
                  )}
                </span>
              ),
            },
            ...(topic
              ? []
              : [{ title: "主题", width: 130, render: (_: unknown, e: MemoryEntry) => e.topic ?? "—" }]),
            {
              title: "来源",
              width: 110,
              render: (_, e) => <Tag color={SOURCE_COLOR[e.source]}>{SOURCE_LABEL[e.source] ?? e.source}</Tag>,
            },
            {
              title: "出处",
              width: 180,
              render: (_, e) => {
                const id = originThread(e);
                if (!id) return <span style={{ color: "var(--fg-3)" }}>{e.source === "user" ? "手动" : "—"}</span>;
                const t = threads.get(id);
                if (t === null) return <span style={{ color: "var(--fg-3)" }}>会话已删除</span>;
                return (
                  <Link href={`/chat/${t?.parent_thread_id ?? id}`} style={{ fontSize: 13 }}>
                    {t?.title || "（未命名会话）"}
                  </Link>
                );
              },
            },
            {
              title: "成立于",
              width: 100,
              render: (_, e) => <span style={{ color: "var(--fg-2)", fontSize: 12 }}>{localDate(e.occurred_at)}</span>,
            },
            {
              title: "",
              width: 168,
              render: (_, e) => (
                <Space size={0}>
                  <Button size="small" type="text" onClick={() => onOpenEntry(e.number)}>
                    演变
                  </Button>
                  {e.status === "active" ? (
                    <>
                      <Button size="small" type="text" onClick={() => setCorrecting(e)}>
                        更正
                      </Button>
                      <Button size="small" type="text" onClick={() => setMoving([e.number])}>
                        移动
                      </Button>
                    </>
                  ) : (
                    <Button
                      size="small"
                      type="text"
                      loading={restore.isPending && restore.variables === e.number}
                      onClick={() => act(restore.mutateAsync(e.number), `已恢复 #${e.number}`)}
                    >
                      恢复
                    </Button>
                  )}
                  <Popconfirm
                    title="彻底删除这条记忆？"
                    description="连同它的取代链和修订历史，不可恢复。只想让智能体不再用它，选「更正」或在修订历史里撤销。"
                    okText="彻底删除"
                    okButtonProps={{ danger: true }}
                    cancelText="取消"
                    onConfirm={() => act(purge.mutateAsync(e.number), "已彻底删除")}
                  >
                    <Button
                      size="small"
                      type="text"
                      danger
                      aria-label={`彻底删除 #${e.number}`}
                      icon={<Icon name="trash" size={14} />}
                    />
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      )}
      <CorrectEntryModal entry={correcting} topics={topics} onClose={() => setCorrecting(null)} />
      <MoveEntriesModal
        numbers={moving}
        topics={topics}
        onClose={() => {
          setMoving(null);
          setSelected([]);
        }}
      />
    </>
  );
}
