"use client";

/**
 * 知识库详情：文档 / 召回测试 / 设置（doc/atlas-rag-design.html §13）。
 * 上传抽屉：选文件 → 分段设置（实时预览）→ 提交后后台入库，列表轮询进度。
 */
import {
  Alert,
  App,
  Button,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Radio,
  Segmented,
  Select,
  Slider,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
  Upload,
} from "antd";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Icon } from "@/components/Icon";
import { relativeTime } from "@/lib/format";
import { can, useMe } from "@/lib/session";

import {
  type Dataset,
  type KbDocument,
  type ProcessRules,
  type RetrievalConfig,
  type StructureRule,
  LEVEL_LABEL,
  METHOD_LABEL,
  STATUS_LABEL,
  kbApi,
  pathLabel,
  useDataset,
  useDocuments,
  useKbMeta,
  useKbMutation,
  useQueries,
} from "./api";
import { RetrievalFunnel } from "./RetrievalFunnel";
import { SharingTab } from "./SharingTab";

export function DatasetPage({ datasetId }: { datasetId: string }) {
  const { data: ds, error } = useDataset(datasetId);
  const [tab, setTab] = useState("documents");

  if (error) return <Alert type="error" showIcon message={(error as Error).message} style={{ margin: 24 }} />;
  if (!ds) return null;
  return (
    <div style={{ flex: 1, overflow: "auto", padding: "20px 28px" }}>
      <Link href="/knowledge" style={{ fontSize: 13, color: "var(--fg-3)" }}>
        ← 知识库
      </Link>
      <div style={{ display: "flex", gap: 12, alignItems: "center", margin: "8px 0 4px" }}>
        <span style={{ fontSize: 28 }}>{ds.icon}</span>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>
            {ds.name}
          </Typography.Title>
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            {ds.description || "暂无描述"}
          </Typography.Text>
        </div>
      </div>
      <Space size={4} style={{ marginBottom: 8 }}>
        {ds.my_level && <Tag>{LEVEL_LABEL[ds.my_level]}</Tag>}
        <Tag>{ds.doc_form === "hierarchical_model" ? "父子分段" : "通用分段"}</Tag>
        <Tag>{METHOD_LABEL[ds.retrieval_config.search_method]}</Tag>
        <Tag>{ds.embedding_model}</Tag>
        {ds.review_required && <Tag color="gold">入库审核</Tag>}
      </Space>
      <Tabs
        activeKey={tab}
        onChange={setTab}
        items={[
          { key: "documents", label: "文档", children: <DocumentsTab ds={ds} /> },
          { key: "hit", label: "召回测试", children: <HitTestingTab ds={ds} /> },
          { key: "sharing", label: "共享", children: <SharingTab ds={ds} /> },
          { key: "settings", label: "设置", children: <SettingsTab ds={ds} /> },
        ]}
      />
    </div>
  );
}

// ═══════════════════════════════════════════════ 文档

function DocumentsTab({ ds }: { ds: Dataset }) {
  const { message } = App.useApp();
  const { data: me } = useMe();
  const [page, setPage] = useState(1);
  const [keyword, setKeyword] = useState<string>();
  const [status, setStatus] = useState<string>();
  const [uploading, setUploading] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const docs = useDocuments(ds.id, { page, keyword, status });
  const act = useKbMutation(async (fn: () => Promise<unknown>) => fn());
  const canReview = ds.can_admin || can(me, "kb:review");

  async function run(fn: () => Promise<unknown>, ok?: string) {
    try {
      await act.mutateAsync(fn);
      if (ok) message.success(ok);
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 12, gap: 8 }}>
        <Space>
          <Input.Search placeholder="搜索文档" allowClear onSearch={(v) => (setKeyword(v || undefined), setPage(1))} />
          <Select
            allowClear
            placeholder="状态"
            style={{ width: 140 }}
            value={status}
            onChange={(v) => (setStatus(v), setPage(1))}
            options={[
              { value: "available", label: "启用中" },
              { value: "disabled", label: "已停用" },
              { value: "archived", label: "已归档" },
              { value: "pending_review", label: "待审核" },
              { value: "error", label: "失败" },
            ]}
          />
        </Space>
        <Space>
          {selected.length > 0 && ds.can_write && (
            <>
              <Button onClick={() => run(() => kbApi.docsStatus(ds.id, "enable", selected), "已启用")}>启用</Button>
              <Button onClick={() => run(() => kbApi.docsStatus(ds.id, "disable", selected), "已停用")}>停用</Button>
              {canReview && ds.review_required && (
                <Button onClick={() => run(() => kbApi.review(ds.id, selected, true), "已审核通过")}>审核通过</Button>
              )}
              <Popconfirm
                title={`删除选中的 ${selected.length} 份文档？`}
                onConfirm={() =>
                  run(() => kbApi.docsStatus(ds.id, "delete", selected), "已删除").then(() => setSelected([]))
                }
              >
                <Button danger>删除</Button>
              </Popconfirm>
            </>
          )}
          {ds.can_write && (
            <Button type="primary" icon={<Icon name="upload" size={14} />} onClick={() => setUploading(true)}>
              添加文档
            </Button>
          )}
        </Space>
      </div>
      <Table<KbDocument>
        rowKey="id"
        size="middle"
        loading={docs.isPending}
        dataSource={docs.data?.data}
        rowSelection={ds.can_write ? { selectedRowKeys: selected, onChange: (k) => setSelected(k as string[]) } : undefined}
        pagination={{ current: page, pageSize: 20, total: docs.data?.total, onChange: setPage, showSizeChanger: false }}
        columns={[
          {
            title: "名称",
            render: (_, d) => (
              <Link href={`/knowledge/${ds.id}/documents/${d.id}`}>
                <Icon name="file" size={14} /> {d.name}
              </Link>
            ),
          },
          { title: "字数", width: 90, render: (_, d) => d.word_count.toLocaleString() },
          {
            title: "分段",
            width: 110,
            render: (_, d) =>
              d.indexing_status === "completed" || !d.segment_count
                ? d.segment_count
                : `${d.completed_segments}/${d.segment_count}`,
          },
          {
            title: "状态",
            width: 200,
            render: (_, d) => (
              <Space size={4} wrap>
                <Tag color={STATUS_LABEL[d.indexing_status].color} title={d.error ?? undefined}>
                  {STATUS_LABEL[d.indexing_status].label}
                </Tag>
                {d.review_status === "pending" && <Tag color="gold">待审核</Tag>}
                {d.review_status === "rejected" && <Tag color="red">已驳回</Tag>}
                {d.archived && <Tag>已归档</Tag>}
              </Space>
            ),
          },
          {
            title: "启用",
            width: 70,
            render: (_, d) => (
              <Switch
                size="small"
                checked={d.enabled}
                disabled={!ds.can_write}
                onChange={(on) => run(() => kbApi.docsStatus(ds.id, on ? "enable" : "disable", [d.id]))}
              />
            ),
          },
          { title: "上传时间", width: 110, render: (_, d) => relativeTime(d.created_at) },
          {
            title: "",
            width: 150,
            render: (_, d) =>
              ds.can_write && (
                <Space size={0}>
                  {d.indexing_status === "error" && (
                    <Button size="small" type="link" onClick={() => run(() => kbApi.docAction(ds.id, d.id, "retry"))}>
                      重试
                    </Button>
                  )}
                  {["waiting", "parsing", "splitting", "indexing"].includes(d.indexing_status) && (
                    <Button size="small" type="link" onClick={() => run(() => kbApi.docAction(ds.id, d.id, "pause"))}>
                      暂停
                    </Button>
                  )}
                  {d.indexing_status === "paused" && (
                    <Button size="small" type="link" onClick={() => run(() => kbApi.docAction(ds.id, d.id, "resume"))}>
                      继续
                    </Button>
                  )}
                  {canReview && d.review_status === "pending" && (
                    <Button size="small" type="link" onClick={() => run(() => kbApi.review(ds.id, [d.id], true), "已审核通过")}>
                      通过
                    </Button>
                  )}
                  <Popconfirm title="删除这份文档？" onConfirm={() => run(() => kbApi.deleteDoc(ds.id, d.id), "已删除")}>
                    <Button size="small" type="link" danger>
                      删除
                    </Button>
                  </Popconfirm>
                </Space>
              ),
          },
        ]}
      />
      <UploadDrawer ds={ds} open={uploading} onClose={() => setUploading(false)} />
    </>
  );
}

// ═══════════════════════════════════════════════ 上传

function UploadDrawer({ ds, open, onClose }: { ds: Dataset; open: boolean; onClose: () => void }) {
  const { message } = App.useApp();
  const meta = useKbMeta();
  const [files, setFiles] = useState<File[]>([]);
  // 通用分段默认按标题：没有标题的文档会自动退回按段落切
  const [mode, setMode] = useState<"structure" | "automatic" | "custom">("structure");
  const [rules, setRules] = useState<ProcessRules>();
  const [preview, setPreview] = useState<Awaited<ReturnType<typeof kbApi.estimate>>>();
  const [busy, setBusy] = useState(false);
  const upload = useKbMutation(({ form }: { form: FormData }) => kbApi.upload(ds.id, form));
  const hierarchical = ds.doc_form === "hierarchical_model";
  const textStructureDefaults: ProcessRules | undefined = meta.data && {
    pre_processing_rules: meta.data.automatic_rules.pre_processing_rules,
    structure: meta.data.structure_text_defaults,
  };
  const effective: ProcessRules | undefined =
    rules ??
    (hierarchical
      ? meta.data?.hierarchical_default_rules
      : mode === "structure"
        ? textStructureDefaults
        : meta.data?.automatic_rules);

  function processRule() {
    if (hierarchical) return { mode: "hierarchical", rules: effective };
    if (mode === "automatic") return { mode: "automatic" };
    return { mode, rules: effective };
  }

  async function doPreview() {
    if (!files[0]) return message.warning("先选择文件");
    const form = new FormData();
    form.append("file", files[0]);
    form.append("doc_form", ds.doc_form);
    form.append("process_rule", JSON.stringify(processRule()));
    setBusy(true);
    try {
      setPreview(await kbApi.estimate(form));
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function submit() {
    if (!files.length) return message.warning("先选择文件");
    const form = new FormData();
    files.forEach((f) => form.append("files", f));
    form.append("doc_form", ds.doc_form);
    form.append("process_rule", JSON.stringify(processRule()));
    try {
      await upload.mutateAsync({ form });
      message.success(`已提交 ${files.length} 份文档，后台入库中`);
      setFiles([]);
      setPreview(undefined);
      onClose();
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  const seg = (key: "segmentation" | "subchunk_segmentation", label: string) => {
    const s = effective?.[key];
    if (!s) return null;
    const set = (patch: Partial<typeof s>) => setRules({ ...effective!, [key]: { ...s, ...patch } });
    return (
      <div style={{ display: "flex", gap: 12, marginBottom: 8 }}>
        <Form.Item label={`${label}分隔符`} style={{ flex: 1, marginBottom: 0 }} extra="\n 表示换行">
          <Input value={s.separator.replace(/\n/g, "\\n")} onChange={(e) => set({ separator: e.target.value })} />
        </Form.Item>
        <Form.Item label="最大长度（字符）" style={{ marginBottom: 0 }}>
          <InputNumber
            min={50}
            max={meta.data?.max_segmentation_length ?? 4000}
            value={s.max_tokens}
            onChange={(v) => set({ max_tokens: v ?? 500 })}
          />
        </Form.Item>
        <Form.Item label="重叠" style={{ marginBottom: 0 }}>
          <InputNumber min={0} max={s.max_tokens - 1} value={s.chunk_overlap} onChange={(v) => set({ chunk_overlap: v ?? 0 })} />
        </Form.Item>
      </div>
    );
  };

  /** 按标题分段的参数 */
  const structureForm = () => {
    const st = effective?.structure;
    if (!st) return null;
    const set = (patch: Partial<StructureRule>) => setRules({ ...effective!, structure: { ...st, ...patch } });
    return (
      <div style={{ display: "flex", gap: 12, marginBottom: 8, flexWrap: "wrap" }}>
        <Form.Item label="切到第几级标题" style={{ marginBottom: 0 }} extra="更深的标题算作正文">
          <InputNumber min={1} max={6} value={st.max_heading_level} onChange={(v) => set({ max_heading_level: v ?? 3 })} />
        </Form.Item>
        <Form.Item
          label={hierarchical ? "父块最大长度（字符）" : "每段最大长度（字符）"}
          style={{ marginBottom: 0 }}
          extra="超长的小节在内部按段落拆开，每块都带标题路径"
        >
          <InputNumber
            min={100}
            max={meta.data?.max_segmentation_length ?? 4000}
            value={st.max_length}
            onChange={(v) => set({ max_length: v ?? 1000 })}
          />
        </Form.Item>
        <Form.Item label="短小节合并阈值" style={{ marginBottom: 0 }} extra="同一上级下的相邻短小节合并；0 = 不合并">
          <InputNumber min={0} max={st.max_length - 1} value={st.min_length} onChange={(v) => set({ min_length: v ?? 0 })} />
        </Form.Item>
      </div>
    );
  };

  /** 按标题分段时子块只需要长度（按结构单元切，不用分隔符） */
  const childLengthForm = () => {
    const s = effective?.subchunk_segmentation;
    if (!s) return null;
    return (
      <Form.Item label="子块最大长度（字符）" extra="子块 = 小节里的段落、列表项、表格行；太短的并入下一条">
        <InputNumber
          min={50}
          max={meta.data?.max_segmentation_length ?? 4000}
          value={s.max_tokens}
          onChange={(v) => setRules({ ...effective!, subchunk_segmentation: { ...s, max_tokens: v ?? 300 } })}
        />
      </Form.Item>
    );
  };

  const preRule = (id: string, label: string) => {
    const on = effective?.pre_processing_rules.find((r) => r.id === id)?.enabled ?? false;
    return (
      <Switch
        size="small"
        checked={on}
        onChange={(v) =>
          setRules({
            ...effective!,
            pre_processing_rules: [
              ...(effective?.pre_processing_rules ?? []).filter((r) => r.id !== id),
              { id, enabled: v },
            ],
          })
        }
        checkedChildren={label}
        unCheckedChildren={label}
      />
    );
  };

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={760}
      title="添加文档"
      extra={
        <Space>
          <Button onClick={doPreview} loading={busy}>
            预览分段
          </Button>
          <Button type="primary" onClick={submit} loading={upload.isPending}>
            保存并入库
          </Button>
        </Space>
      }
    >
      <Upload.Dragger
        multiple
        beforeUpload={(f) => {
          setFiles((prev) => [...prev, f]);
          return false;
        }}
        onRemove={(f) => setFiles((prev) => prev.filter((x) => x.name !== f.name))}
        fileList={files.map((f) => ({ uid: f.name, name: f.name, status: "done" as const }))}
        accept={meta.data?.supported_extensions.map((e) => `.${e}`).join(",")}
      >
        <p style={{ margin: 0 }}>点击或拖拽文件到这里</p>
        <p style={{ margin: 0, fontSize: 12, color: "var(--fg-3)" }}>
          支持 {meta.data?.supported_extensions.join("、")}；单个文件不超过{" "}
          {Math.round((meta.data?.upload_max_bytes ?? 0) / 1024 / 1024)}MB
        </p>
      </Upload.Dragger>

      <Typography.Title level={5} style={{ marginTop: 20 }}>
        分段设置
      </Typography.Title>
      <Form layout="vertical">
        {!hierarchical && (
          <Form.Item>
            <Radio.Group value={mode} onChange={(e) => (setMode(e.target.value), setRules(undefined), setPreview(undefined))}>
              <Radio value="structure">按标题（推荐）：一个小节一段，带标题路径</Radio>
              <Radio value="automatic">自动（按行切，500 字符，重叠 50）</Radio>
              <Radio value="custom">自定义</Radio>
            </Radio.Group>
          </Form.Item>
        )}
        {(hierarchical || mode !== "automatic") && effective && (
          <>
            {hierarchical && (
              <Form.Item label="父块">
                <Radio.Group
                  value={effective.parent_mode}
                  onChange={(e) =>
                    setRules({
                      ...effective,
                      parent_mode: e.target.value,
                      segmentation: effective.segmentation ?? meta.data?.hierarchical_default_rules.segmentation,
                      structure: effective.structure ?? meta.data?.hierarchical_default_rules.structure,
                    })
                  }
                >
                  <Radio value="structure">按标题（推荐）：一个标题小节一个父块，子块带标题路径</Radio>
                  <Radio value="paragraph">按段落：父块按规则切分</Radio>
                  <Radio value="full_doc">全文：整篇作为一个父块（适合短文档）</Radio>
                </Radio.Group>
              </Form.Item>
            )}
            {((!hierarchical && mode === "structure") || (hierarchical && effective.parent_mode === "structure")) &&
              structureForm()}
            {((!hierarchical && mode === "custom") || (hierarchical && effective.parent_mode === "paragraph")) &&
              seg("segmentation", hierarchical ? "父块" : "")}
            {hierarchical &&
              (effective.parent_mode === "structure" ? childLengthForm() : seg("subchunk_segmentation", "子块"))}
            <Space style={{ marginTop: 8 }}>
              {preRule("remove_extra_spaces", "去除多余空白")}
              {preRule("remove_urls_emails", "去除链接与邮箱")}
            </Space>
          </>
        )}
      </Form>

      {preview && (
        <>
          <Typography.Title level={5} style={{ marginTop: 20 }}>
            分段预览 <Typography.Text type="secondary">（共 {preview.total_segments} 段，展示前 10 段）</Typography.Text>
          </Typography.Title>
          {preview.preview.map((p, i) => (
            <div key={i} style={{ border: "1px solid var(--line)", borderRadius: 6, padding: 10, marginBottom: 8, fontSize: 13 }}>
              <div style={{ color: "var(--fg-3)", fontSize: 12, marginBottom: 4 }}>
                #{i + 1} · {p.word_count} 字{p.child_count != null && ` · ${p.child_count} 个子块`}
              </div>
              {p.title_path?.length > 0 && <Tag color="blue" style={{ marginBottom: 4 }}>{pathLabel(p.title_path)}</Tag>}
              <div style={{ whiteSpace: "pre-wrap", maxHeight: 160, overflow: "auto" }}>{p.content}</div>
              {p.child_chunks && (
                <Space size={4} wrap style={{ marginTop: 6 }}>
                  {p.child_chunks.map((c, j) => (
                    <Tag key={j} style={{ whiteSpace: "normal" }}>
                      {c}
                    </Tag>
                  ))}
                </Space>
              )}
            </div>
          ))}
        </>
      )}
    </Drawer>
  );
}

// ═══════════════════════════════════════════════ 召回测试

function HitTestingTab({ ds }: { ds: Dataset }) {
  const { message } = App.useApp();
  const [query, setQuery] = useState("");
  const [cfg, setCfg] = useState<RetrievalConfig>(ds.retrieval_config);
  const [result, setResult] = useState<Awaited<ReturnType<typeof kbApi.hitTesting>>>();
  const hit = useKbMutation(() => kbApi.hitTesting(ds.id, query, cfg));
  const history = useQueries(ds.id);
  const [view, setView] = useState<"records" | "funnel">("records");
  // 「最近检索」里点开的那次检索的过程
  const [opened, setOpened] = useState<string>();

  async function run() {
    if (!query.trim()) return;
    try {
      setView("records");
      setResult(await hit.mutateAsync(undefined));
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  return (
    <div style={{ display: "flex", gap: 24 }}>
      <div style={{ width: 380, flex: "none" }}>
        <Input.TextArea
          rows={4}
          placeholder="输入一个用户可能问的问题"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          maxLength={2000}
        />
        <div style={{ margin: "12px 0" }}>
          <RetrievalForm value={cfg} onChange={setCfg} compact />
        </div>
        <Button type="primary" block onClick={run} loading={hit.isPending}>
          测试
        </Button>
        <Typography.Title level={5} style={{ marginTop: 20 }}>
          最近检索
        </Typography.Title>
        {(history.data?.data ?? []).map((q) => (
          <div
            key={q.id}
            style={{ fontSize: 13, padding: "6px 0", borderBottom: "1px solid var(--line)", cursor: "pointer" }}
            onClick={() => setQuery(q.query)}
          >
            <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{q.query}</div>
            <div style={{ fontSize: 12, color: "var(--fg-3)" }}>
              {q.source} · 命中 {q.hits} · {relativeTime(q.created_at)}
              {q.retrieval_id && (
                <a
                  style={{ marginLeft: 8 }}
                  onClick={(e) => {
                    e.stopPropagation();
                    setOpened(q.retrieval_id ?? undefined);
                  }}
                >
                  检索过程
                </a>
              )}
            </div>
          </div>
        ))}
        <Drawer title="检索过程" width="min(1100px, 90vw)" open={!!opened} onClose={() => setOpened(undefined)} destroyOnHidden>
          {opened && <RetrievalFunnel id={opened} />}
        </Drawer>
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        {result && (
          <Segmented
            size="small"
            style={{ marginBottom: 8 }}
            value={view}
            onChange={(v) => setView(v as "records" | "funnel")}
            options={[
              { label: "检索结果", value: "records" },
              { label: "检索过程", value: "funnel" },
            ]}
          />
        )}
        {!result ? (
          <Empty description="命中的分段会显示在这里" style={{ marginTop: 60 }} />
        ) : view === "funnel" ? (
          <RetrievalFunnel id={result.retrieval_id} />
        ) : !result.records.length ? (
          <Empty description="没有命中任何分段（可以看看「检索过程」，或调低阈值、换个问法）" style={{ marginTop: 60 }} />
        ) : (
          <>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {result.records.length} 个分段 · {result.elapsed_ms}ms
            </Typography.Text>
            {result.records.map((r) => (
              <div key={r.segment_uuid} style={{ border: "1px solid var(--line)", borderRadius: 6, padding: 12, margin: "8px 0" }}>
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, color: "var(--fg-3)" }}>
                  <span>
                    {r.document_name} · 第 {r.position} 段
                    {r.title_path?.length > 0 && <Tag color="blue" style={{ marginLeft: 8 }}>{pathLabel(r.title_path)}</Tag>}
                  </span>
                  <Tag color="blue">{r.score.toFixed(3)}</Tag>
                </div>
                <div style={{ whiteSpace: "pre-wrap", fontSize: 13, marginTop: 6 }}>{r.content}</div>
                {r.child_chunks.length > 0 && (
                  <div style={{ marginTop: 8, fontSize: 12 }}>
                    命中子块：
                    {r.child_chunks.map((c, i) => (
                      <Tag key={i} color="geekblue" style={{ whiteSpace: "normal", marginTop: 4 }}>
                        {c.score.toFixed(3)} · {c.content}
                      </Tag>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
}

/** 检索设置（召回测试与设置页共用） */
function RetrievalForm({
  value,
  onChange,
  compact,
}: {
  value: RetrievalConfig;
  onChange: (v: RetrievalConfig) => void;
  compact?: boolean;
}) {
  const meta = useKbMeta();
  const set = (patch: Partial<RetrievalConfig>) => onChange({ ...value, ...patch });
  const hybrid = value.search_method === "hybrid_search";
  const rerank = hybrid || value.reranking_enable;
  const vw = value.weights.vector_setting.vector_weight;
  return (
    <Form layout="vertical" size={compact ? "small" : "middle"}>
      <Form.Item label="检索方式" style={{ marginBottom: 8 }}>
        <Radio.Group
          value={value.search_method}
          onChange={(e) => set({ search_method: e.target.value })}
          options={Object.entries(METHOD_LABEL).map(([v, label]) => ({ value: v, label }))}
        />
      </Form.Item>
      <Space wrap>
        <Form.Item label="Top K" style={{ marginBottom: 8 }}>
          <InputNumber min={1} max={20} value={value.top_k} onChange={(v) => set({ top_k: v ?? 4 })} />
        </Form.Item>
        <Form.Item label="分数阈值" style={{ marginBottom: 8 }}>
          <Space>
            <Switch
              size="small"
              checked={value.score_threshold_enabled}
              onChange={(v) => set({ score_threshold_enabled: v })}
            />
            <InputNumber
              min={0}
              max={1}
              step={0.05}
              disabled={!value.score_threshold_enabled}
              value={value.score_threshold}
              onChange={(v) => set({ score_threshold: v ?? 0.5 })}
            />
          </Space>
        </Form.Item>
      </Space>
      <Form.Item label={hybrid ? "重排（混合检索必须重排）" : "重排"} style={{ marginBottom: 8 }}>
        <Space direction="vertical" style={{ width: "100%" }}>
          {!hybrid && (
            <Switch size="small" checked={value.reranking_enable} onChange={(v) => set({ reranking_enable: v })} />
          )}
          {rerank && (
            <Radio.Group
              value={value.reranking_mode}
              onChange={(e) =>
                set({
                  reranking_mode: e.target.value,
                  reranking_model:
                    e.target.value === "reranking_model" && !value.reranking_model.model
                      ? meta.data?.rerank_models[0] ?? value.reranking_model
                      : value.reranking_model,
                })
              }
            >
              <Radio value="weighted_score">加权（向量 + 关键词）</Radio>
              <Radio value="reranking_model">重排模型</Radio>
            </Radio.Group>
          )}
          {rerank && value.reranking_mode === "weighted_score" && (
            <div>
              <div style={{ fontSize: 12, color: "var(--fg-3)" }}>
                语义 {vw.toFixed(1)} · 关键词 {(1 - vw).toFixed(1)}
              </div>
              <Slider
                min={0}
                max={1}
                step={0.1}
                value={vw}
                onChange={(v) =>
                  set({
                    weights: {
                      vector_setting: { vector_weight: Math.round(v * 10) / 10 },
                      keyword_setting: { keyword_weight: Math.round((1 - v) * 10) / 10 },
                    },
                  })
                }
              />
            </div>
          )}
          {rerank && value.reranking_mode === "reranking_model" && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {value.reranking_model.model || meta.data?.rerank_models[0]?.model}（首次使用需下载模型，约 1GB）
            </Typography.Text>
          )}
        </Space>
      </Form.Item>
    </Form>
  );
}

// ═══════════════════════════════════════════════ 设置

function SettingsTab({ ds }: { ds: Dataset }) {
  const { message } = App.useApp();
  const router = useRouter();
  const [form] = Form.useForm();
  const [cfg, setCfg] = useState<RetrievalConfig>(ds.retrieval_config);
  const save = useKbMutation((body: Record<string, unknown>) => kbApi.updateDataset(ds.id, body));
  const del = useKbMutation(() => kbApi.deleteDataset(ds.id));
  const readOnly = !ds.can_write;

  async function submit() {
    const v = await form.validateFields();
    // 可见范围改在「共享」里（用户中心数据权限），这里不再提交
    const body: Record<string, unknown> = { ...v, retrieval_config: cfg };
    try {
      await save.mutateAsync(body);
      message.success("已保存");
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  return (
    <div style={{ maxWidth: 640 }}>
      <Form
        form={form}
        layout="vertical"
        disabled={readOnly}
        initialValues={{
          name: ds.name,
          description: ds.description,
          icon: ds.icon,
          review_required: ds.review_required,
        }}
      >
        <div style={{ display: "flex", gap: 12 }}>
          <Form.Item name="icon" label="图标" style={{ width: 80 }}>
            <Input maxLength={4} />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true }]} style={{ flex: 1 }}>
            <Input maxLength={255} />
          </Form.Item>
        </div>
        <Form.Item name="description" label="描述">
          <Input.TextArea rows={3} maxLength={2000} />
        </Form.Item>
        <Form.Item name="review_required" label="入库审核" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item label="向量模型" extra="更换向量模型需要重建索引，当前版本不支持修改。">
          <Input value={ds.embedding_model} disabled />
        </Form.Item>
      </Form>
      <Typography.Title level={5}>默认检索设置</Typography.Title>
      <RetrievalForm value={cfg} onChange={setCfg} />
      <Space style={{ marginTop: 12 }}>
        <Button type="primary" onClick={submit} loading={save.isPending} disabled={readOnly}>
          保存
        </Button>
        {ds.can_admin && (
          <Popconfirm
            title="删除知识库？"
            description="文档、分段与索引会一起删除，绑定它的智能体将无法检索。"
            onConfirm={async () => {
              try {
                await del.mutateAsync(undefined);
                router.replace("/knowledge");
              } catch (e) {
                message.error((e as Error).message);
              }
            }}
          >
            <Button danger>删除知识库</Button>
          </Popconfirm>
        )}
      </Space>
    </div>
  );
}
