"use client";

/** 文档详情：分段列表（搜索、启停、编辑、新增、删除；父子模式展示子块）与处理信息。 */
import { Alert, App, Button, Descriptions, Empty, Input, Modal, Pagination, Popconfirm, Space, Switch, Tag, Typography } from "antd";
import Link from "next/link";
import { useState } from "react";

import { relativeTime } from "@/lib/format";

import {
  type Segment,
  STATUS_LABEL,
  kbApi,
  pathLabel,
  useDataset,
  useKbDocument,
  useKbMutation,
  useSegments,
} from "./api";

export function DocumentPage({ datasetId, documentId }: { datasetId: string; documentId: string }) {
  const { message } = App.useApp();
  const ds = useDataset(datasetId);
  const doc = useKbDocument(datasetId, documentId);
  const [page, setPage] = useState(1);
  const [keyword, setKeyword] = useState<string>();
  const segs = useSegments(datasetId, documentId, { page, keyword });
  const [editing, setEditing] = useState<Segment | "new">();
  const [text, setText] = useState("");
  const act = useKbMutation(async (fn: () => Promise<unknown>) => fn());
  const canWrite = !!ds.data?.can_write && doc.data?.indexing_status === "completed";

  async function run(fn: () => Promise<unknown>, ok?: string) {
    try {
      await act.mutateAsync(fn);
      if (ok) message.success(ok);
      return true;
    } catch (e) {
      message.error((e as Error).message);
      return false;
    }
  }

  if (doc.error) return <Alert type="error" showIcon message={(doc.error as Error).message} style={{ margin: 24 }} />;
  const d = doc.data;
  const rules = d?.process_rule?.rules;

  return (
    <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>
      <div style={{ flex: 1, overflow: "auto", padding: "20px 28px" }}>
        <Link href={`/knowledge/${datasetId}`} style={{ fontSize: 13, color: "var(--fg-3)" }}>
          ← {ds.data?.name ?? "知识库"}
        </Link>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", margin: "8px 0 12px" }}>
          <Typography.Title level={4} style={{ margin: 0 }}>
            {d?.name}
          </Typography.Title>
          <Space>
            <Input.Search placeholder="搜索分段" allowClear onSearch={(v) => (setKeyword(v || undefined), setPage(1))} />
            {canWrite && (
              <Button onClick={() => (setEditing("new"), setText(""))} type="primary">
                添加分段
              </Button>
            )}
          </Space>
        </div>
        {d && d.indexing_status !== "completed" && (
          <Alert
            type={d.indexing_status === "error" ? "error" : "info"}
            showIcon
            style={{ marginBottom: 12 }}
            message={`文档${STATUS_LABEL[d.indexing_status].label}${d.error ? `：${d.error}` : ""}`}
            description={d.indexing_status === "error" ? undefined : "入库完成后才能编辑分段。"}
          />
        )}
        {!segs.data?.data.length ? (
          <Empty description="没有分段" style={{ marginTop: 60 }} />
        ) : (
          segs.data.data.map((s) => (
            <div
              key={s.id}
              style={{
                border: "1px solid var(--line)",
                borderRadius: 6,
                padding: 12,
                marginBottom: 10,
                opacity: s.enabled ? 1 : 0.55,
              }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, color: "var(--fg-3)" }}>
                <span>
                  #{s.position} · {s.word_count} 字 · 命中 {s.hit_count} 次{s.status !== "completed" && ` · ${s.status}`}
                </span>
                <Space size={4}>
                  <Switch
                    size="small"
                    checked={s.enabled}
                    disabled={!canWrite}
                    onChange={(on) => run(() => kbApi.updateSegment(datasetId, documentId, s.id, { enabled: on }))}
                  />
                  {canWrite && (
                    <>
                      <Button size="small" type="link" onClick={() => (setEditing(s), setText(s.content))}>
                        编辑
                      </Button>
                      <Popconfirm
                        title="删除这个分段？"
                        onConfirm={() => run(() => kbApi.deleteSegment(datasetId, documentId, s.id), "已删除")}
                      >
                        <Button size="small" type="link" danger>
                          删除
                        </Button>
                      </Popconfirm>
                    </>
                  )}
                </Space>
              </div>
              {s.title_path?.length > 0 && (
                <Tag color="blue" style={{ marginTop: 6 }}>
                  {pathLabel(s.title_path)}
                </Tag>
              )}
              <div style={{ whiteSpace: "pre-wrap", fontSize: 13, marginTop: 6 }}>{s.content}</div>
              {s.child_chunks && s.child_chunks.length > 0 && (
                <Space size={4} wrap style={{ marginTop: 8 }}>
                  {s.child_chunks.map((c) => (
                    <Tag key={c.id} style={{ whiteSpace: "normal" }} title={`子块 #${c.position}`}>
                      {c.content}
                    </Tag>
                  ))}
                </Space>
              )}
            </div>
          ))
        )}
        <Pagination
          current={page}
          pageSize={20}
          total={segs.data?.total ?? 0}
          onChange={setPage}
          showSizeChanger={false}
          hideOnSinglePage
          style={{ marginTop: 12 }}
        />
      </div>

      <div style={{ width: 300, flex: "none", borderLeft: "1px solid var(--line)", padding: 20, overflow: "auto" }}>
        <Typography.Title level={5}>文档信息</Typography.Title>
        {d && (
          <Descriptions column={1} size="small">
            <Descriptions.Item label="状态">
              <Tag color={STATUS_LABEL[d.indexing_status].color}>{STATUS_LABEL[d.indexing_status].label}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label="审核">
              {{ none: "不需要", pending: "待审核", approved: "已通过", rejected: "已驳回" }[d.review_status]}
            </Descriptions.Item>
            <Descriptions.Item label="分段">{d.segment_count}</Descriptions.Item>
            <Descriptions.Item label="字数">{d.word_count.toLocaleString()}</Descriptions.Item>
            <Descriptions.Item label="大小">{((d.data_source_info.size ?? 0) / 1024).toFixed(1)} KB</Descriptions.Item>
            <Descriptions.Item label="入库耗时">{d.indexing_latency != null ? `${d.indexing_latency}s` : "—"}</Descriptions.Item>
            <Descriptions.Item label="上传">{relativeTime(d.created_at)}</Descriptions.Item>
            <Descriptions.Item label="分段规则">
              {d.process_rule?.mode === "automatic"
                ? "自动"
                : d.process_rule?.mode === "structure" || rules?.parent_mode === "structure"
                  ? d.process_rule?.mode === "hierarchical"
                    ? "父子 · 按标题"
                    : "按标题"
                  : d.process_rule?.mode === "hierarchical"
                    ? "父子"
                    : "自定义"}
            </Descriptions.Item>
            {rules?.structure && (
              <Descriptions.Item label="标题层级">
                切到第 {rules.structure.max_heading_level} 级 · 最长 {rules.structure.max_length} 字
              </Descriptions.Item>
            )}
            {rules?.segmentation && (
              <Descriptions.Item label={rules.parent_mode ? "父块" : "分段"}>
                {rules.segmentation.max_tokens} 字符 / 重叠 {rules.segmentation.chunk_overlap}
              </Descriptions.Item>
            )}
            {rules?.parent_mode === "full_doc" && <Descriptions.Item label="父块">全文</Descriptions.Item>}
            {rules?.subchunk_segmentation && (
              <Descriptions.Item label="子块">{rules.subchunk_segmentation.max_tokens} 字符</Descriptions.Item>
            )}
          </Descriptions>
        )}
      </div>

      <Modal
        open={!!editing}
        title={editing === "new" ? "添加分段" : `编辑分段 #${(editing as Segment | undefined)?.position ?? ""}`}
        onCancel={() => setEditing(undefined)}
        confirmLoading={act.isPending}
        width={680}
        onOk={async () => {
          const ok =
            editing === "new"
              ? await run(() => kbApi.createSegment(datasetId, documentId, text), "已添加")
              : await run(() => kbApi.updateSegment(datasetId, documentId, (editing as Segment).id, { content: text }), "已保存");
          if (ok) setEditing(undefined);
        }}
      >
        {d?.doc_form === "hierarchical_model" && (
          <Alert type="info" showIcon style={{ marginBottom: 8 }} message="保存后会按文档的子块规则重新切分子块。" />
        )}
        <Input.TextArea rows={12} value={text} onChange={(e) => setText(e.target.value)} />
      </Modal>
    </div>
  );
}
