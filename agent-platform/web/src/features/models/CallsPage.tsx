"use client";

/**
 * 调用记录：每次经模型网关的调用一条。个人看自己 Key 的，管理员可以切到「全部」（本企业）。
 *
 *   汇总   按模型 / 按 Key / 按天：次数、出错、四项用量（输入、缓存写入、缓存命中、输出）、花费
 *   明细   时间、模型、Key、结果、试了哪几条线路、首字用时、总用时、用量、花费；展开看每次尝试
 *
 * ★ 用量以供应商回的为准；回答中断、没收到用量时是估算的，标「估」。
 * ★ 不存请求和回答的内容。
 */
import { Alert, Button, Empty, Segmented, Select, Space, Table, Tag } from "antd";
import { useMemo, useState } from "react";

import {
  type MgCall,
  type MgCallFilter,
  type MgCallSummary,
  type MgKey,
  useMgCalls,
  useMgCallSummary,
  useMgKeys,
  useMgModels,
  useMgMyKeys,
} from "@/api/model-gateway";
import pageStyles from "@/features/agents/agents.module.css";
import styles from "@/features/catalog/catalog.module.css";
import { MgGate } from "./ModelsPage";
import { fmtTime, money } from "./shared";

export function CallsPage() {
  return <MgGate>{(me) => <Calls manage={me.can_manage} />}</MgGate>;
}

const RANGES = [
  { value: "today", label: "今天" },
  { value: "7", label: "7 天" },
  { value: "30", label: "30 天" },
  { value: "month", label: "本月" },
];

function since(range: string): string {
  const now = new Date();
  if (range === "today") return new Date(now.getFullYear(), now.getMonth(), now.getDate()).toISOString();
  if (range === "month") return new Date(now.getFullYear(), now.getMonth(), 1).toISOString();
  return new Date(now.getTime() - Number(range) * 86_400_000).toISOString();
}

const STATUS: Record<MgCall["status"], { label: string; color: string }> = {
  ok: { label: "成功", color: "success" },
  error: { label: "出错", color: "error" },
  interrupted: { label: "中途中断", color: "warning" },
};

/** 一次尝试的结果（网关的失败分类） */
const OUTCOME: Record<string, string> = {
  ok: "成功",
  bad_request: "请求有误",
  rate_limited: "限流",
  overloaded: "过载",
  auth_failed: "密钥失效",
  no_balance: "余额不足",
  timeout: "超时",
  interrupted: "中断",
};

/** 整次调用的错误分类（网关回给调用方的） */
const ERROR: Record<string, string> = {
  key_invalid: "Key 无效",
  model_forbidden: "Key 不能用这个模型",
  beyond_capability: "超出模型能力",
  format_unsupported: "模型没有这种格式的线路",
  bad_request: "请求有误",
  key_limit: "超出 Key 的限额",
  model_busy: "模型繁忙",
  no_route: "没有可用线路",
  upstream_error: "上游出错",
  upstream_timeout: "上游超时",
  interrupted: "回答中断",
};

const FORMAT: Record<string, string> = { anthropic: "Anthropic", openai: "OpenAI", google: "Gemini" };
const pathText = (p: string) => (p === "direct" ? "同格式直通" : p.startsWith("convert:") ? `转成 ${FORMAT[p.slice(8)] ?? p.slice(8)} 格式` : p);

const ms = (v: number | null) => (v == null ? "—" : v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${v}ms`);
const num = (n: number) => n.toLocaleString();

function Calls({ manage }: { manage: boolean }) {
  const [scope, setScope] = useState<"mine" | "all">(manage ? "all" : "mine");
  const [range, setRange] = useState("7");
  const [model, setModel] = useState<string>();
  const [status, setStatus] = useState<string>();
  const [by, setBy] = useState<"model" | "key" | "day">("model");
  // 时间范围按选的那一刻算，切换范围时才变：不然每次渲染查询键都不同
  const sinceAt = useMemo(() => since(range), [range]);
  const filter: MgCallFilter = { scope, model, status, since: sinceAt };
  const callsQ = useMgCalls(filter);
  const summaryQ = useMgCallSummary(by, filter);
  const modelsQ = useMgModels();
  const myKeysQ = useMgMyKeys();
  const allKeysQ = useMgKeys({}, manage && scope === "all");
  const keyName = useMemo(() => {
    const map = new Map<string, MgKey>();
    for (const k of [...(myKeysQ.data ?? []), ...(allKeysQ.data ?? [])]) map.set(k.id, k);
    return (id: string | null) => {
      const k = id ? map.get(id) : undefined;
      if (!k) return id ? "（已删除）" : "—";
      return k.key_type === "client" ? k.name || k.client_code || "" : `${k.name}${k.label ? ` · ${k.label}` : ""}`;
    };
  }, [myKeysQ.data, allKeysQ.data]);
  const calls = (callsQ.data?.pages ?? []).flatMap((p) => p.data);

  return (
    <div className={pageStyles.page}>
      <header className={pageStyles.head}>
        <div className={pageStyles.titleRow}>
          <h1 className={pageStyles.title}>调用记录</h1>
        </div>
        <div className={pageStyles.toolbar}>
          {manage && (
            <Segmented
              value={scope}
              onChange={(v) => setScope(v as "mine" | "all")}
              options={[
                { value: "all", label: "全部" },
                { value: "mine", label: "我的 Key" },
              ]}
            />
          )}
          <Segmented value={range} onChange={(v) => setRange(v as string)} options={RANGES} />
          <Select
            allowClear
            showSearch
            placeholder="全部模型"
            style={{ width: 200 }}
            value={model}
            onChange={setModel}
            options={(modelsQ.data ?? []).map((m) => ({ value: m.name, label: m.display_name }))}
          />
          <Select
            allowClear
            placeholder="全部结果"
            style={{ width: 120 }}
            value={status}
            onChange={setStatus}
            options={Object.entries(STATUS).map(([value, s]) => ({ value, label: s.label }))}
          />
        </div>
      </header>
      <div className={pageStyles.body}>
        <div className={styles.secTitle} style={{ display: "flex", alignItems: "center", gap: 12 }}>
          汇总
          <Segmented
            size="small"
            value={by}
            onChange={(v) => setBy(v as typeof by)}
            options={[
              { value: "model", label: "按模型" },
              { value: "key", label: "按 Key" },
              { value: "day", label: "按天" },
            ]}
          />
        </div>
        {summaryQ.error && <Alert type="error" showIcon style={{ marginBottom: 12 }} message={summaryQ.error.message} />}
        <Table<MgCallSummary>
          size="small"
          rowKey="group"
          loading={summaryQ.isPending}
          dataSource={summaryQ.data ?? []}
          pagination={false}
          locale={{ emptyText: <Empty description="这段时间没有调用" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
          columns={[
            { title: by === "model" ? "模型" : by === "key" ? "Key" : "日期", render: (_, s) => (by === "key" ? s.label : s.group) },
            { title: "次数", width: 90, render: (_, s) => num(s.calls) },
            {
              title: "出错",
              width: 90,
              render: (_, s) => (s.errors ? <span style={{ color: "var(--danger)" }}>{num(s.errors)}</span> : <span className={styles.muted}>0</span>),
            },
            { title: "输入", width: 110, render: (_, s) => num(s.input) },
            { title: "缓存写入", width: 110, render: (_, s) => num(s.cache_write) },
            { title: "缓存命中", width: 110, render: (_, s) => num(s.cache_hit) },
            { title: "输出", width: 110, render: (_, s) => num(s.output) },
            { title: "花费", width: 110, render: (_, s) => money(s.cost) },
          ]}
        />

        <div className={styles.secTitle}>明细</div>
        {callsQ.error && <Alert type="error" showIcon style={{ marginBottom: 12 }} message={callsQ.error.message} />}
        <Table<MgCall>
          size="small"
          rowKey="id"
          loading={callsQ.isPending}
          dataSource={calls}
          pagination={false}
          locale={{ emptyText: <Empty description="没有调用记录" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
          expandable={{ expandedRowRender: (c) => <Attempts call={c} />, rowExpandable: (c) => c.attempts.length > 0 }}
          columns={[
            { title: "时间", width: 140, render: (_, c) => fmtTime(c.started_at) },
            { title: "模型", render: (_, c) => <code>{c.model}</code> },
            { title: "Key", width: 180, render: (_, c) => keyName(c.key_id) },
            {
              title: "结果",
              width: 150,
              render: (_, c) => (
                <span>
                  <Tag color={STATUS[c.status].color}>{STATUS[c.status].label}</Tag>
                  {c.attempts.length > 1 && <span className={styles.muted}>试了 {c.attempts.length} 条</span>}
                  {c.error_category && <div className={styles.desc2}>{ERROR[c.error_category] ?? c.error_category}</div>}
                </span>
              ),
            },
            { title: "首字", width: 70, render: (_, c) => ms(c.first_content_ms) },
            { title: "总用时", width: 80, render: (_, c) => ms(c.duration_ms) },
            {
              title: "用量（输入 / 缓存 / 输出）",
              width: 200,
              render: (_, c) => (
                <span>
                  {num(c.input_tokens + c.cache_write_tokens)} / {num(c.cache_hit_tokens)} / {num(c.output_tokens)}
                  {c.estimated && (
                    <Tag style={{ marginLeft: 4 }} title="没收到供应商回的用量（比如回答中断），按内容长度估的">
                      估
                    </Tag>
                  )}
                </span>
              ),
            },
            { title: "花费", width: 90, render: (_, c) => money(c.cost) },
          ]}
        />
        {callsQ.hasNextPage && (
          <Space style={{ marginTop: 12 }}>
            <Button loading={callsQ.isFetchingNextPage} onClick={() => void callsQ.fetchNextPage()}>
              加载更多
            </Button>
          </Space>
        )}
      </div>
    </div>
  );
}

function Attempts({ call: c }: { call: MgCall }) {
  const notes = c.notes ?? {};
  return (
    <div>
      {c.attempts.map((a, i) => (
        <div key={`${a.route}-${i}`} className={styles.kv}>
          <span>
            第 {i + 1} 次 · {a.provider} · <code>{a.upstream_model}</code>
            {a.path && <span className={styles.muted}> · {pathText(a.path)}</span>}
          </span>
          <span>
            <Tag color={a.outcome === "ok" ? "success" : "error"}>{OUTCOME[a.outcome] ?? (a.outcome || "—")}</Tag>
            {ms(a.ms)}
            {a.message && <span className={styles.muted}> · {a.message}</span>}
          </span>
        </div>
      ))}
      {Array.isArray(notes.dropped) && notes.dropped.length > 0 && (
        <p className={styles.muted}>上游不支持、没发过去的参数：{(notes.dropped as string[]).join("、")}</p>
      )}
      {Array.isArray(notes.params) && notes.params.length > 0 && (
        <p className={styles.muted}>参数调整：{(notes.params as string[]).join("；")}</p>
      )}
      {c.session && <p className={styles.muted}>会话：{c.session}</p>}
    </div>
  );
}
