"use client";

/**
 * 知识库详情：文档 / 召回测试 / 设置（doc/atlas-rag-design.html §13）。知识库属于一个知识集。
 * 上传抽屉：选文件 → 分段规则（预览）→ 提交后后台入库，列表轮询进度；也用它对单个文档重新分段。
 */
import {
  Alert,
  App,
  Breadcrumb,
  Button,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
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
import { useEffect, useState } from "react";

import { Icon } from "@/components/Icon";
import { relativeTime } from "@/lib/format";
import { useMe } from "@/lib/session";

import {
  type Dataset,
  type KbDocument,
  type ProcessRules,
  type RetrievalConfig,
  type StructureRule,
  type Visibility,
  DOC_FORM_LABEL,
  LEVEL_LABEL,
  METHOD_LABEL,
  STATUS_LABEL,
  kbApi,
  pathLabel,
  useDataset,
  useDocuments,
  useKbMeta,
  useKbMutation,
  useKnowledgeSet,
  useKnowledgeSets,
  useQueries,
} from "./api";
import { RetrievalFunnel } from "./RetrievalFunnel";
import { ICON_BOX, VisibilityTag } from "./SetPage";
import { SharingTab } from "./SharingTab";

export function DatasetPage({ datasetId }: { datasetId: string }) {
  const { data: ds, error } = useDataset(datasetId);
  const { data: me } = useMe();
  const set = useKnowledgeSet(ds?.set_id ?? "");
  const [tab, setTab] = useState("documents");
  const [sharing, setSharing] = useState(false);
  // 新建知识库后直接上传文档（SetPage 跳过来时带 ?upload=1）
  const [uploading, setUploading] = useState(false);
  useEffect(() => {
    if (!new URLSearchParams(window.location.search).get("upload")) return;
    setUploading(true);
    // 只在刚建完时打开一次：刷新不再弹出
    window.history.replaceState(null, "", window.location.pathname);
  }, []);

  if (error) return <Alert type="error" showIcon message={(error as Error).message} style={{ margin: 24 }} />;
  if (!ds) return null;
  const setName = set.data?.name ?? "知识集";
  return (
    <div style={{ flex: 1, overflow: "auto", padding: "18px 32px 48px" }}>
      <Breadcrumb
        items={[
          { title: <Link href="/knowledge">知识集</Link> },
          { title: <Link href={`/knowledge/sets/${ds.set_id}`}>{setName}</Link> },
          { title: ds.name },
        ]}
      />
      <div style={{ display: "flex", gap: 12, alignItems: "flex-start", marginTop: 12 }}>
        <span style={ICON_BOX}>{ds.icon}</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <Typography.Title level={4} style={{ margin: 0 }}>
            {ds.name}
            <span style={{ marginLeft: 8, verticalAlign: 3 }}>
              <VisibilityTag v={ds.visibility} />
            </span>
          </Typography.Title>
          <div style={{ fontSize: 13, color: "var(--fg-3)", marginTop: 2 }}>
            {ds.description || "（没有描述）"}
          </div>
          <div style={{ fontSize: 12.5, color: "var(--fg-3)", marginTop: 4 }}>
            属于知识集「{setName}」 · 引用方式：{DOC_FORM_LABEL[ds.doc_form]} · {ds.document_count} 份文档 ·{" "}
            {ds.word_count.toLocaleString()} 字 ·{" "}
            {ds.owner_id === me?.id ? "我创建的" : ds.my_level ? `我${LEVEL_LABEL[ds.my_level]}` : ""}
          </div>
        </div>
        {ds.owner_kind !== "service" && (
          <Button icon={<Icon name="share" size={14} />} onClick={() => setSharing(true)}>
            共享
          </Button>
        )}
        {ds.can_write && (
          <Button type="primary" icon={<Icon name="upload" size={14} />} onClick={() => setUploading(true)}>
            上传文档
          </Button>
        )}
      </div>
      <Tabs
        style={{ marginTop: 12 }}
        activeKey={tab}
        onChange={setTab}
        items={[
          { key: "documents", label: "文档", children: <DocumentsTab ds={ds} /> },
          { key: "hit", label: "检索测试", children: <HitTestingTab ds={ds} /> },
          ...(ds.can_write ? [{ key: "settings", label: "设置", children: <SettingsTab ds={ds} /> }] : []),
        ]}
      />
      <Modal open={sharing} title="共享" footer={null} width={680} onCancel={() => setSharing(false)} destroyOnHidden>
        <SharingTab ds={ds} />
      </Modal>
      <UploadDrawer ds={ds} open={uploading} onClose={() => setUploading(false)} />
    </div>
  );
}

// ═══════════════════════════════════════════════ 文档

function DocumentsTab({ ds }: { ds: Dataset }) {
  const { message } = App.useApp();
  const [page, setPage] = useState(1);
  const [keyword, setKeyword] = useState<string>();
  const [status, setStatus] = useState<string>();
  // 重新分段的那份文档
  const [redo, setRedo] = useState<KbDocument>();
  const [selected, setSelected] = useState<string[]>([]);
  const docs = useDocuments(ds.id, { page, keyword, status });
  const act = useKbMutation(async (fn: () => Promise<unknown>) => fn());

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
              { value: "error", label: "失败" },
            ]}
          />
        </Space>
        <Space>
          {selected.length > 0 && ds.can_write && (
            <>
              <Button onClick={() => run(() => kbApi.docsStatus(ds.id, "enable", selected), "已启用")}>启用</Button>
              <Button onClick={() => run(() => kbApi.docsStatus(ds.id, "disable", selected), "已停用")}>停用</Button>
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
                {d.archived && <Tag>已归档</Tag>}
                {!d.is_current && <Tag>已被新版本取代</Tag>}
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
            width: 190,
            render: (_, d) =>
              ds.can_write && (
                <Space size={0}>
                  {d.is_current && ["completed", "error"].includes(d.indexing_status) && (
                    <Button size="small" type="link" onClick={() => setRedo(d)}>
                      重新分段
                    </Button>
                  )}
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
      {redo && <UploadDrawer ds={ds} redo={redo} open onClose={() => setRedo(undefined)} />}
    </>
  );
}

// ═══════════════════════════════════════════════ 上传

function UploadDrawer({
  ds,
  open,
  redo,
  onClose,
}: {
  ds: Dataset;
  open: boolean;
  /** 给了 = 对这份文档重新分段（用原文件，不用选） */
  redo?: KbDocument;
  onClose: () => void;
}) {
  const { message } = App.useApp();
  const meta = useKbMeta();
  const [files, setFiles] = useState<File[]>([]);
  // 自动 = 按文档结构切（有标题、条款按它们切，没有就按段落）；自定义 = 自己定长度、分隔符
  const [mode, setMode] = useState<"automatic" | "custom">("automatic");
  const [rules, setRules] = useState<ProcessRules>();
  const [preview, setPreview] = useState<Awaited<ReturnType<typeof kbApi.estimate>>>();
  const [busy, setBusy] = useState(false);
  const upload = useKbMutation(({ form }: { form: FormData }) => kbApi.upload(ds.id, form));
  const resegment = useKbMutation((rule: unknown) => kbApi.resegment(ds.id, redo!.id, rule));
  const hierarchical = ds.doc_form === "hierarchical_model";
  // 自定义的起点：父子分段用默认规则；通用分段用分隔符切
  const effective: ProcessRules | undefined =
    rules ??
    (hierarchical
      ? meta.data?.hierarchical_default_rules
      : meta.data && {
          pre_processing_rules: meta.data.automatic_rules.pre_processing_rules,
          segmentation: { separator: "\n\n", max_tokens: 800, chunk_overlap: 50 },
        });

  function processRule() {
    if (mode === "automatic") return { mode: hierarchical ? "hierarchical" : "automatic" };
    return { mode: hierarchical ? "hierarchical" : "custom", rules: effective };
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
    try {
      if (redo) {
        await resegment.mutateAsync(processRule());
        message.success("已开始重新分段，处理完之前旧的分段照常能查");
      } else {
        if (!files.length) return message.warning("先选择文件");
        const form = new FormData();
        files.forEach((f) => form.append("files", f));
        form.append("doc_form", ds.doc_form);
        form.append("process_rule", JSON.stringify(processRule()));
        await upload.mutateAsync({ form });
        message.success(`已提交 ${files.length} 份文档，处理完就能被查到`);
      }
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
          <Input value={s.separator.replace(/\n/g, "\\n")} onChange={(e) => set({ separator: e.target.value.replace(/\\n/g, "\n") })} />
        </Form.Item>
        <Form.Item label={`${label}最长（字）`} style={{ marginBottom: 0 }}>
          <InputNumber
            min={50}
            max={meta.data?.max_segmentation_length ?? 4000}
            value={s.max_tokens}
            onChange={(v) => set({ max_tokens: v ?? 500 })}
          />
        </Form.Item>
        <Form.Item label="相邻段重叠（字）" style={{ marginBottom: 0 }}>
          <InputNumber min={0} max={s.max_tokens - 1} value={s.chunk_overlap} onChange={(v) => set({ chunk_overlap: v ?? 0 })} />
        </Form.Item>
      </div>
    );
  };

  /** 按章节：节怎么切、按几级标题、一节最长、用来匹配的小段最长 */
  const sectionForm = () => {
    if (!effective) return null;
    const st = effective.structure ?? meta.data?.hierarchical_default_rules.structure;
    const child = effective.subchunk_segmentation;
    const setSt = (patch: Partial<StructureRule>) => setRules({ ...effective, structure: { ...st!, ...patch } });
    return (
      <>
        <div style={{ display: "flex", gap: 12, marginBottom: 8, flexWrap: "wrap" }}>
          <Form.Item label="节怎么切" style={{ marginBottom: 0 }}>
            <Select
              style={{ width: 200 }}
              value={effective.parent_mode}
              onChange={(parent_mode) =>
                setRules({
                  ...effective,
                  parent_mode,
                  segmentation: effective.segmentation ?? meta.data?.hierarchical_default_rules.segmentation,
                  structure: st,
                })
              }
              options={[
                { value: "structure", label: "按标题和条款（推荐）" },
                { value: "paragraph", label: "按段落" },
                { value: "full_doc", label: "整篇文档作为一节" },
              ]}
            />
          </Form.Item>
          {effective.parent_mode === "structure" && st && (
            <>
              <Form.Item label="按几级标题切" style={{ marginBottom: 0 }} extra="更深的标题算作正文">
                <InputNumber min={1} max={6} value={st.max_heading_level} onChange={(v) => setSt({ max_heading_level: v ?? 3 })} />
              </Form.Item>
              <Form.Item label="一节最长（字）" style={{ marginBottom: 0 }} extra="超长的节在内部按段落拆开，每块都带标题路径">
                <InputNumber
                  min={100}
                  max={meta.data?.max_segmentation_length ?? 4000}
                  value={st.max_length}
                  onChange={(v) => setSt({ max_length: v ?? 3000 })}
                />
              </Form.Item>
            </>
          )}
        </div>
        {effective.parent_mode === "paragraph" && seg("segmentation", "一节")}
        {child && (
          <Form.Item label="用来匹配的小段最长（字）" extra="小段 = 节里的款、项、段落、表格行；太短的并入下一条">
            <InputNumber
              min={50}
              max={meta.data?.max_segmentation_length ?? 4000}
              value={child.max_tokens}
              onChange={(v) => setRules({ ...effective, subchunk_segmentation: { ...child, max_tokens: v ?? 300 } })}
            />
          </Form.Item>
        )}
      </>
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
      title={redo ? `重新分段 · ${redo.name}` : `上传文档 · ${ds.name}`}
      extra={
        <Space>
          {!redo && (
            <Button onClick={doPreview} loading={busy}>
              预览分段
            </Button>
          )}
          <Button type="primary" onClick={submit} loading={upload.isPending || resegment.isPending}>
            开始处理
          </Button>
        </Space>
      }
    >
      <Typography.Title level={5} style={{ marginTop: 0 }}>
        ① 文件
      </Typography.Title>
      {redo ? (
        <Typography.Text>
          <Icon name="file" size={14} /> {redo.name}
          <Typography.Text type="secondary"> · 用新的分段规则重新处理，处理完之前旧的分段照常能查</Typography.Text>
        </Typography.Text>
      ) : (
        <Upload.Dragger
          multiple
          height={140}
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
      )}

      <Typography.Title level={5} style={{ marginTop: 20 }}>
        ② 分段规则
      </Typography.Title>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12.5 }}>
        这个知识库的引用方式是「{DOC_FORM_LABEL[ds.doc_form]}」：
        {hierarchical ? "按标题和条款切成节，节里的款、项是用来匹配的小段" : "切成独立的段落"}
      </Typography.Paragraph>
      <Form layout="vertical">
        <Form.Item>
          <Radio.Group value={mode} onChange={(e) => (setMode(e.target.value), setRules(undefined), setPreview(undefined))}>
            <Space direction="vertical">
              <Radio value="automatic">
                <b>自动（推荐）</b>
                <div style={{ fontSize: 12.5, color: "var(--fg-3)" }}>
                  按文档结构切：有标题（Markdown、Word 的标题，或「第 X 章 / 第 X 节 / 第 X 条」）就按标题和条款切；
                  没有标题就按段落切，太短的合并、太长的再切。大多数文档不用改
                </div>
              </Radio>
              <Radio value="custom">
                <b>自定义</b>
                <div style={{ fontSize: 12.5, color: "var(--fg-3)" }}>自己指定怎么切和长度，适合格式特殊的文档</div>
              </Radio>
            </Space>
          </Radio.Group>
        </Form.Item>
        {mode === "custom" && effective && (
          <>
            {hierarchical ? sectionForm() : seg("segmentation", "每段")}
            <Space style={{ marginTop: 8 }}>
              {preRule("remove_extra_spaces", "去除多余空白")}
              {preRule("remove_urls_emails", "去除链接与邮箱")}
            </Space>
          </>
        )}
      </Form>

      {!redo && (
        <Typography.Title level={5} style={{ marginTop: 20 }}>
          ③ 预览
          {preview && (
            <Typography.Text type="secondary" style={{ fontWeight: 400, fontSize: 13 }}>
              {" "}
              预计切成 {preview.total_segments} {hierarchical ? "节" : "段"}，展示前 {preview.preview.length} 个
            </Typography.Text>
          )}
        </Typography.Title>
      )}
      {!redo && !preview && (
        <Typography.Text type="secondary" style={{ fontSize: 12.5 }}>
          先点右上角「预览分段」看看会切成什么样，再开始处理
        </Typography.Text>
      )}
      {preview?.preview.map((p, i) => (
        <div key={i} style={{ border: "1px solid var(--line)", borderRadius: 6, padding: 10, marginBottom: 8, fontSize: 13 }}>
          <div style={{ color: "var(--fg-3)", fontSize: 12, marginBottom: 4 }}>
            #{i + 1} · {p.word_count} 字{p.child_count != null && ` · 含 ${p.child_count} 个用来匹配的小段`}
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
  hideTopK,
}: {
  value: RetrievalConfig;
  onChange: (v: RetrievalConfig) => void;
  compact?: boolean;
  /** 设置页不给：智能体每次带回几段由所属知识集定 */
  hideTopK?: boolean;
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
        {!hideTopK && (
          <Form.Item label="Top K" style={{ marginBottom: 8 }}>
            <InputNumber min={1} max={20} value={value.top_k} onChange={(v) => set({ top_k: v ?? 4 })} />
          </Form.Item>
        )}
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
  const sets = useKnowledgeSets();
  const save = useKbMutation((body: Record<string, unknown>) => kbApi.updateDataset(ds.id, body));
  const del = useKbMutation(() => kbApi.deleteDataset(ds.id));
  const readOnly = !ds.can_write;
  // 只能移到自己维护的知识集（还要是这个库的所有者）
  const movable = (sets.data?.data ?? []).filter((x) => x.can_manage || x.id === ds.set_id);

  async function submit() {
    const v = await form.validateFields();
    const body: Record<string, unknown> = { ...v, retrieval_config: cfg };
    // 可见性、所属知识集只有所有者能改；没改就不提交
    if (v.visibility === ds.visibility) delete body.visibility;
    if (v.set_id === ds.set_id) delete body.set_id;
    try {
      await save.mutateAsync(body);
      message.success("已保存");
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  return (
    <div style={{ maxWidth: 680 }}>
      <Form
        form={form}
        layout="vertical"
        disabled={readOnly}
        initialValues={{
          name: ds.name,
          description: ds.description,
          icon: ds.icon,
          visibility: ds.visibility,
          set_id: ds.set_id,
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
        <Form.Item
          name="description"
          label="介绍"
          extra="智能体用 knowledge_list 看知识集里有哪些库时会看到，帮它在库多时挑出相关的几个"
        >
          <Input.TextArea rows={2} maxLength={2000} />
        </Form.Item>
        <Form.Item
          name="visibility"
          label="可见性"
          extra={ds.can_admin ? undefined : "只有知识库的所有者能改"}
        >
          <Radio.Group disabled={!ds.can_admin}>
            <Space direction="vertical">
              <Radio value={"public" satisfies Visibility}>公有：大家都能用，所有人都能在智能体里查到</Radio>
              <Radio value={"private" satisfies Visibility}>
                私有：有数据权限的人才能用（在右上角「共享」里授权）；智能体按使用者的权限判断
              </Radio>
            </Space>
          </Radio.Group>
        </Form.Item>
        <Form.Item label="引用方式" extra="新建时选定，上传文档后不能改">
          <b>{DOC_FORM_LABEL[ds.doc_form]}</b>
          {ds.doc_form === "hierarchical_model" && <span style={{ color: "var(--fg-3)" }}>（父子分段）</span>}
        </Form.Item>
        <Form.Item
          name="set_id"
          label="所属知识集"
          extra="只能移到你维护的知识集。移走后，只选了原知识集的智能体就查不到这个库了"
        >
          <Select
            style={{ width: 320 }}
            disabled={!ds.can_admin}
            loading={sets.isPending}
            options={movable.map((x) => ({ value: x.id, label: `${x.icon} ${x.name}` }))}
          />
        </Form.Item>
        <Form.Item label="向量模型" extra="更换向量模型需要重建索引，当前版本不支持修改。">
          <Input value={ds.embedding_model} disabled />
        </Form.Item>
      </Form>
      <Typography.Title level={5}>检索设置</Typography.Title>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12.5 }}>
        智能体查这个库时用的检索方式和相关度门槛；门槛按这个库的向量模型打的分判断。每次带回几段由所属知识集定。
      </Typography.Paragraph>
      <RetrievalForm value={cfg} onChange={setCfg} hideTopK />
      <Space style={{ marginTop: 12 }}>
        <Button type="primary" onClick={submit} loading={save.isPending} disabled={readOnly}>
          保存
        </Button>
        {ds.can_admin && (
          <Popconfirm
            title="删除知识库？"
            description="文档和分段会一起删除，智能体将不再能查到这些内容。"
            onConfirm={async () => {
              try {
                await del.mutateAsync(undefined);
                router.replace(`/knowledge/sets/${ds.set_id}`);
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
