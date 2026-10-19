"use client";

/**
 * 一次检索的过程（漏斗）：每个候选分段在各环节的名次与分数，以及它在哪一环掉的。
 * 排查「为什么没检索到 / 为什么排在后面」—— 是没召回、重排后被截断，还是被分数阈值滤掉。
 * 有链路 id 时可以跳到 Jaeger 看各环节耗时（地址模板来自服务端设置）。
 */
import { Alert, Empty, Spin, Table, Tag, Tooltip, Typography } from "antd";

import { relativeTime } from "@/lib/format";

import { type FunnelRow, type FunnelStatus, METHOD_LABEL, pathLabel, useRetrieval } from "./api";

const STAGE_LABEL: Record<string, string> = {
  semantic: "语义召回",
  full_text: "全文召回",
  merged: "合并",
  rerank: "重排",
  threshold: "分数阈值",
  query_understanding: "查询理解",
  diversity: "多样性",
  assemble: "归并到分段",
  final: "取前 K 个",
};

const STATUS: Record<FunnelStatus, { label: string; color: string; hint: string }> = {
  selected: { label: "入选", color: "green", hint: "进入最终结果" },
  truncated: { label: "排名靠后", color: "orange", hint: "召回到了，但排名在前 K 个之外被截断" },
  below_threshold: { label: "低于阈值", color: "red", hint: "重排后的分数低于分数阈值，被过滤" },
  missing: { label: "分段已删除", color: "default", hint: "向量库里还有，分段已删除或停用" },
  dropped: { label: "中途丢弃", color: "default", hint: "在中间环节丢失" },
};

export function RetrievalFunnel({ id }: { id: string }) {
  const { data, isLoading, error } = useRetrieval(id);
  if (isLoading) return <Spin style={{ display: "block", marginTop: 40 }} />;
  if (error) return <Alert type="error" message={(error as Error).message} />;
  if (!data) return null;

  const stages = data.stages;
  const columns = [
    {
      title: "结果",
      key: "status",
      width: 96,
      fixed: "left" as const,
      render: (_: unknown, r: FunnelRow) => {
        const s = STATUS[r.status];
        const at = r.dropped_at != null ? stages[r.dropped_at] : null;
        return (
          <Tooltip title={at ? `${s.hint}（在「${STAGE_LABEL[at.name] ?? at.name}」这一环）` : s.hint}>
            <Tag color={s.color}>{r.final_rank ? `#${r.final_rank} ${s.label}` : s.label}</Tag>
          </Tooltip>
        );
      },
    },
    {
      title: "分段",
      key: "segment",
      width: 300,
      render: (_: unknown, r: FunnelRow) =>
        r.deleted ? (
          <Typography.Text type="secondary">（已删除）{r.segment_uuid.slice(0, 8)}</Typography.Text>
        ) : (
          <div style={{ fontSize: 12 }}>
            <div style={{ color: "var(--fg-3)" }}>
              {data.datasets.length > 1 && `${r.dataset_name} · `}
              {r.document_name} · 第 {r.position} 段
            </div>
            {r.title_path.length > 0 && (
              <Tag color="blue" style={{ margin: "2px 0" }}>
                {pathLabel(r.title_path)}
              </Tag>
            )}
            <Tooltip title={<div style={{ whiteSpace: "pre-wrap" }}>{r.content}</div>}>
              <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 300 }}>
                {r.content}
              </div>
            </Tooltip>
          </div>
        ),
    },
    ...stages.map((st, i) => ({
      title: (
        <Tooltip title={stageHint(st.attrs)}>
          <div style={{ fontSize: 12, lineHeight: 1.4 }}>
            {STAGE_LABEL[st.name] ?? st.name}
            {data.datasets.length > 1 && st.dataset_name && (
              <div style={{ color: "var(--fg-3)" }}>{st.dataset_name}</div>
            )}
            <div style={{ color: "var(--fg-3)", fontWeight: "normal" }}>
              {st.count} 个 · {Math.round(st.elapsed_ms)}ms
            </div>
          </div>
        </Tooltip>
      ),
      key: `s${i}`,
      width: 96,
      render: (_: unknown, r: FunnelRow) => {
        const c = r.cells[i];
        if (!c) return <span style={{ color: r.dropped_at === i ? "var(--danger, #cf1322)" : "var(--fg-4, #ccc)" }}>{r.dropped_at === i ? "✕" : "—"}</span>;
        return (
          <div style={{ fontSize: 12, lineHeight: 1.4 }}>
            <b>#{c.rank}</b> {c.score.toFixed(3)}
            {c.points > 1 && <div style={{ color: "var(--fg-3)" }}>{c.points} 个子块</div>}
          </div>
        );
      },
    })),
  ];

  return (
    <div>
      <div style={{ display: "flex", gap: 12, alignItems: "baseline", flexWrap: "wrap", marginBottom: 8 }}>
        <Typography.Text strong>{data.query}</Typography.Text>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {METHOD_LABEL[data.config.search_method]} · top_k {data.config.top_k}
          {data.config.score_threshold_enabled && ` · 阈值 ${data.config.score_threshold}`}
          {" · "}
          {data.latency_ms}ms · {data.source} · {relativeTime(data.created_at)}
        </Typography.Text>
        {data.trace_url ? (
          <a href={data.trace_url} target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>
            查看链路 ↗
          </a>
        ) : (
          data.trace_id && (
            <Typography.Text type="secondary" copyable style={{ fontSize: 12 }}>
              {data.trace_id}
            </Typography.Text>
          )
        )}
      </div>
      {data.rows.length === 0 ? (
        <Empty description="各路召回都没有候选" />
      ) : (
        <Table
          size="small"
          rowKey="segment_uuid"
          columns={columns}
          dataSource={data.rows}
          pagination={false}
          scroll={{ x: 400 + stages.length * 96 }}
        />
      )}
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8 }}>
        每格是该分段在这一环的名次与分数（父子分段时取命中子块里最好的）；✕ 表示在这一环被去掉。
        想要的分段不在表里 = 各路召回都没进候选（没召回）：多半是分段切得不好或问法与原文差异大，
        可以试试混合检索、调大 top_k，或回到文档检查分段。
        {data.truncated && " 候选过多，只列出前 200 个分段。"}
      </Typography.Paragraph>
    </div>
  );
}

function stageHint(attrs: Record<string, unknown>): string | undefined {
  const parts = Object.entries(attrs)
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => `${k}: ${String(v)}`);
  return parts.length ? parts.join("，") : undefined;
}
