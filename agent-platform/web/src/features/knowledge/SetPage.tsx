"use client";

/**
 * 知识集详情：知识库 / 检索测试 / 设置（doc/design/knowledge-set-design.html）。
 *
 * ★ 知识集只是分组，不改变权限：集里的库谁能用仍按库的公有 / 私有；这里只列我能读的库。
 * ★ 检索设置分两半：每次带回几段归知识集（维护人定），检索方式和门槛归各个库（库的所有者定）。
 */
import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  App,
  Breadcrumb,
  Button,
  Card,
  Checkbox,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Radio,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Icon } from "@/components/Icon";
import { relativeTime } from "@/lib/format";
import { can, useMe } from "@/lib/session";

import {
  type Dataset,
  type DocForm,
  type KnowledgeSet,
  type Visibility,
  DOC_FORM_LABEL,
  directory,
  kbApi,
  pathLabel,
  useDatasets,
  useKbMutation,
  useKnowledgeSet,
} from "./api";
import { DESCRIPTION_HELP, DescriptionLabel, NO_DESCRIPTION, maintainerLine } from "./KnowledgeListPage";

export function SetPage({ setId }: { setId: string }) {
  const { data: set, error } = useKnowledgeSet(setId);
  const { data: me } = useMe();
  const datasets = useDatasets(setId);
  const [tab, setTab] = useState("datasets");
  const [creating, setCreating] = useState(false);

  if (error) return <Alert type="error" showIcon message={(error as Error).message} style={{ margin: 24 }} />;
  if (!set) return null;
  const rows = datasets.data?.data ?? [];
  return (
    <div style={{ flex: 1, overflow: "auto", padding: "18px 32px 48px" }}>
      <Breadcrumb items={[{ title: <Link href="/knowledge">知识集</Link> }, { title: set.name }]} />
      <div style={{ display: "flex", gap: 12, alignItems: "flex-start", marginTop: 12 }}>
        <span style={ICON_BOX}>{set.icon}</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <Typography.Title level={4} style={{ margin: 0 }}>
            {set.name}
            <Tag color={set.can_manage ? "blue" : undefined} style={{ marginLeft: 8, verticalAlign: 3 }}>
              {set.can_manage ? "我维护" : "可查阅"}
            </Tag>
          </Typography.Title>
          <div style={{ fontSize: 13, marginTop: 4, color: set.description ? "var(--fg-2)" : "var(--warning, #b7791f)" }}>
            {set.description || NO_DESCRIPTION}
          </div>
          <div style={{ fontSize: 12.5, color: "var(--fg-3)", marginTop: 4 }}>
            {maintainerLine(set)} · {set.dataset_count} 个知识库 · {set.document_count} 份文档 · 更新于{" "}
            {relativeTime(set.updated_at)}
          </div>
        </div>
        {set.can_manage && can(me, "kb:create") && (
          <Button type="primary" icon={<Icon name="plus" size={14} />} onClick={() => setCreating(true)}>
            新建知识库
          </Button>
        )}
      </div>
      <Tabs
        style={{ marginTop: 12 }}
        activeKey={tab}
        onChange={setTab}
        items={[
          {
            key: "datasets",
            label: `知识库 ${rows.length}`,
            children: <DatasetsTab rows={rows} loading={datasets.isPending} />,
          },
          { key: "hit", label: "检索测试", children: <SetHitTab set={set} rows={rows} /> },
          ...(set.can_manage
            ? [{ key: "settings", label: "设置", children: <SetSettingsTab set={set} rows={rows} /> }]
            : []),
        ]}
      />
      {creating && <CreateDatasetModal set={set} onClose={() => setCreating(false)} />}
    </div>
  );
}

export const ICON_BOX: React.CSSProperties = {
  width: 44,
  height: 44,
  borderRadius: 10,
  background: "var(--primary-soft)",
  display: "grid",
  placeItems: "center",
  fontSize: 22,
  flex: "none",
};

export function VisibilityTag({ v }: { v: Visibility }) {
  return v === "public" ? <Tag color="success">公有</Tag> : <Tag color="warning">私有</Tag>;
}

// ═══════════════════════════════════════════════ 知识库

function DatasetsTab({ rows, loading }: { rows: Dataset[]; loading: boolean }) {
  const router = useRouter();
  return (
    <>
      <Table<Dataset>
        rowKey="id"
        size="middle"
        loading={loading}
        dataSource={rows}
        pagination={false}
        locale={{ emptyText: "这个知识集里还没有你能查阅的知识库" }}
        onRow={(d) => ({ onClick: () => router.push(`/knowledge/${d.id}`), style: { cursor: "pointer" } })}
        columns={[
          {
            title: "知识库",
            render: (_, d) => (
              <Space>
                <span style={{ fontSize: 18 }}>{d.icon}</span>
                <div>
                  <b>{d.name}</b>
                  <div style={{ fontSize: 12, color: "var(--fg-3)" }}>{d.description}</div>
                </div>
              </Space>
            ),
          },
          { title: "引用方式", width: 100, render: (_, d) => DOC_FORM_LABEL[d.doc_form] },
          { title: "文档", width: 80, render: (_, d) => d.document_count },
          { title: "可见性", width: 90, render: (_, d) => <VisibilityTag v={d.visibility} /> },
          { title: "更新", width: 110, render: (_, d) => relativeTime(d.updated_at ?? d.created_at) },
        ]}
      />
      <Typography.Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 8 }}>
        公有的库大家都能用；私有的库要有数据权限才能用，你没有权限的私有库不会列在这里。知识集只是分组，不改变权限。
      </Typography.Text>
    </>
  );
}

// ═══════════════════════════════════════════════ 检索测试

function SetHitTab({ set, rows }: { set: KnowledgeSet; rows: Dataset[] }) {
  const { message } = App.useApp();
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [result, setResult] = useState<Awaited<ReturnType<typeof kbApi.retrieve>>>();
  const hit = useKbMutation(() =>
    kbApi.retrieve(picked.length ? picked : rows.map((d) => d.id), query.trim(), set.top_k),
  );
  const names = rows.filter((d) => picked.includes(d.id)).map((d) => `"${d.name}"`);

  async function run() {
    if (!query.trim() || !rows.length) return;
    try {
      setResult(await hit.mutateAsync(undefined));
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  return (
    <div style={{ display: "flex", gap: 24 }}>
      <div style={{ width: 380, flex: "none" }}>
        <Input.TextArea
          rows={3}
          placeholder="输入一个问题，看看智能体能查到什么"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          maxLength={2000}
        />
        <div style={{ fontSize: 12.5, color: "var(--fg-2)", margin: "12px 0 6px" }}>只查这几个库（不选就查整个知识集）</div>
        <Checkbox.Group value={picked} onChange={(v) => setPicked(v as string[])} style={{ display: "grid", gap: 4 }}>
          {rows.map((d) => (
            <Checkbox key={d.id} value={d.id}>
              {d.icon} {d.name}
            </Checkbox>
          ))}
        </Checkbox.Group>
        <Button type="primary" block style={{ marginTop: 12 }} onClick={run} loading={hit.isPending} disabled={!rows.length}>
          测试
        </Button>
        <div style={{ fontSize: 12, color: "var(--fg-3)", marginTop: 10 }}>
          智能体调用时等同于：
          <code>
            knowledge_search(知识集=&quot;{set.name}&quot;, 检索词=…{names.length ? `, 知识库=[${names.join(", ")}]` : ""})
          </code>
          ，最多带回 {set.top_k} 段
        </div>
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        {!result ? (
          <Empty description="命中的分段会显示在这里" style={{ marginTop: 60 }} />
        ) : !result.records.length ? (
          <Empty description="没有命中任何分段（换个问法，或去库的检索测试里看检索过程）" style={{ marginTop: 60 }} />
        ) : (
          <>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {result.records.length} 个分段 · {result.elapsed_ms}ms
            </Typography.Text>
            {result.records.map((r) => (
              <div key={r.segment_uuid} style={{ border: "1px solid var(--line)", borderRadius: 6, padding: 12, margin: "8px 0" }}>
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, color: "var(--fg-3)" }}>
                  <span>
                    <Tag color="blue">库：{r.dataset_name}</Tag>
                    {r.document_name}
                    {r.title_path?.length > 0 && <span style={{ marginLeft: 8 }}>{pathLabel(r.title_path)}</span>}
                  </span>
                  <span>{r.score.toFixed(3)}</span>
                </div>
                <div style={{ whiteSpace: "pre-wrap", fontSize: 13, marginTop: 6 }}>{r.content}</div>
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════ 设置

/** 智能体在提示词里看到的那一行（与服务端 providers/knowledge.py::catalog 一致） */
export function catalogLine(name: string, description: string, datasetNames: string[]): string {
  const desc = description.replace(/\s+/g, " ").trim();
  const more = datasetNames.length > 5 ? ` 等 ${datasetNames.length} 个` : "";
  return `· ${name}：${desc || `包含知识库：${datasetNames.slice(0, 5).join("、")}${more}`}`;
}

function SetSettingsTab({ set, rows }: { set: KnowledgeSet; rows: Dataset[] }) {
  const { message } = App.useApp();
  const router = useRouter();
  const [form] = Form.useForm();
  const [q, setQ] = useState("");
  const users = useQuery({ queryKey: ["directory", "users", q], queryFn: () => directory.users(q), staleTime: 30_000 });
  const save = useKbMutation((body: Record<string, unknown>) => kbApi.updateSet(set.id, body));
  const del = useKbMutation(() => kbApi.deleteSet(set.id));
  const name = Form.useWatch("name", form) ?? set.name;
  const description = Form.useWatch("description", form) ?? set.description;
  const known = new Map([...set.maintainers, ...(users.data ?? [])].map((u) => [u.id, u.name]));

  async function submit() {
    const v = await form.validateFields();
    try {
      await save.mutateAsync(v);
      message.success("已保存");
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  return (
    <div style={{ maxWidth: 720 }}>
      <Form
        form={form}
        layout="vertical"
        initialValues={{
          icon: set.icon,
          name: set.name,
          description: set.description,
          top_k: set.top_k,
          maintainers: set.maintainers.map((m) => m.id),
        }}
      >
        <div style={{ display: "flex", gap: 12 }}>
          <Form.Item name="icon" label="图标" style={{ width: 80 }}>
            <Input maxLength={4} />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: "请填写名称" }]} style={{ flex: 1 }}>
            <Input maxLength={40} />
          </Form.Item>
        </div>
        <Form.Item name="description" label={<DescriptionLabel />} extra={DESCRIPTION_HELP} style={{ marginBottom: 6 }}>
          <Input.TextArea rows={3} maxLength={200} showCount placeholder="写清里面有什么、能回答哪类问题" />
        </Form.Item>
        <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 8 }}>
          <Button
            size="small"
            disabled={!rows.length}
            onClick={() =>
              form.setFieldValue(
                "description",
                rows
                  .map((d) => (d.description ? `${d.name}（${d.description.replace(/\s+/g, " ").trim()}）` : d.name))
                  .join("；")
                  .slice(0, 200),
              )
            }
          >
            按知识库生成初稿
          </Button>
        </div>
        <Card size="small" style={{ background: "var(--bg-2, #fafafa)", marginBottom: 4 }}>
          <code style={{ fontSize: 12, whiteSpace: "pre-wrap" }}>
            {catalogLine(name, description ?? "", rows.map((d) => d.name))}
          </code>
        </Card>
        <Typography.Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 16 }}>
          ↑ 智能体在提示词的「可查的知识」里看到的样子
        </Typography.Text>
        <Form.Item
          name="top_k"
          label="每次最多引用几段"
          extra="智能体在这个知识集里查一次，最多带回几段。检索方式和相关度门槛由各知识库的所有者在库的设置里定"
        >
          <InputNumber min={1} max={20} />
        </Form.Item>
        <Form.Item
          name="maintainers"
          label="维护人"
          rules={[{ required: true, message: "至少要有一个维护人" }]}
          extra="维护人可以改介绍和检索设置、在里面新建知识库。不改变谁能用（按各知识库的公有 / 私有）"
        >
          <Select
            mode="multiple"
            showSearch
            filterOption={false}
            onSearch={setQ}
            placeholder="搜索同事"
            options={[...known].map(([id, n]) => ({ value: id, label: n }))}
          />
        </Form.Item>
        <Button type="primary" onClick={submit} loading={save.isPending}>
          保存
        </Button>
      </Form>
      <Card size="small" style={{ marginTop: 24, borderColor: "#f5c2c2" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <div style={{ flex: 1 }}>
            <b>删除知识集</b>
            <div style={{ fontSize: 12.5, color: "var(--fg-3)" }}>
              {set.dataset_count
                ? `还有 ${set.dataset_count} 个知识库：先把它们移到别的知识集或删除`
                : "选了它的智能体将不再能查到这些内容"}
            </div>
          </div>
          <Popconfirm
            title="删除这个知识集？"
            disabled={set.dataset_count > 0}
            onConfirm={async () => {
              try {
                await del.mutateAsync(undefined);
                router.replace("/knowledge");
              } catch (e) {
                message.error((e as Error).message);
              }
            }}
          >
            <Button danger disabled={set.dataset_count > 0}>
              删除
            </Button>
          </Popconfirm>
        </div>
      </Card>
    </div>
  );
}

// ═══════════════════════════════════════════════ 新建知识库

const FORMS: Record<DocForm, { desc: string; eg: string; tech: string }> = {
  hierarchical_model: {
    desc: "用小段去匹配，返回它所在的整节或整条，上下文完整。适合制度、法规、手册、技术文档这类有标题或条款的长文档",
    eg: "问「年假几天」→ 返回整条「第三条 职工累计工作已满1年不满10年的，年休假5天；已满10年……」",
    tech: "即父子分段：节（条）是父块，节里的款、项、段落是子块",
  },
  text_model: {
    desc: "每段独立，返回最相关的几段。适合 FAQ、问答对、产品短文、通知公告",
    eg: "问「专业版多少钱」→ 返回「专业版每月 199 元，含 20 个席位」这一段",
    tech: "即普通分段：每段一个向量，没有父子",
  },
};

function CreateDatasetModal({ set, onClose }: { set: KnowledgeSet; onClose: () => void }) {
  const router = useRouter();
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const create = useKbMutation(kbApi.createDataset);

  async function submit() {
    const v = await form.validateFields();
    try {
      const ds = await create.mutateAsync({ ...v, set_id: set.id });
      onClose();
      router.push(`/knowledge/${ds.id}?upload=1`);
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  return (
    <Modal
      open
      title={`在「${set.name}」里新建知识库`}
      okText="创建并上传文档"
      onOk={submit}
      onCancel={onClose}
      confirmLoading={create.isPending}
      width={620}
    >
      <Form
        form={form}
        layout="vertical"
        initialValues={{ icon: "📚", visibility: "private" as Visibility, doc_form: "hierarchical_model" as DocForm }}
      >
        <div style={{ display: "flex", gap: 12 }}>
          <Form.Item name="icon" label="图标" style={{ width: 80 }}>
            <Input maxLength={4} />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: "请填写名称" }]} style={{ flex: 1 }}>
            <Input maxLength={255} placeholder="例：员工手册" />
          </Form.Item>
        </div>
        <Form.Item name="description" label="介绍" extra="知识集里库多时，智能体看库的介绍挑出相关的几个">
          <Input.TextArea rows={2} maxLength={2000} placeholder="这个库里有什么" />
        </Form.Item>
        <Form.Item name="visibility" label="可见性">
          <Radio.Group>
            <Space direction="vertical">
              <Radio value="public">公有：大家都能用</Radio>
              <Radio value="private">私有：有数据权限的人才能用；创建后在「共享」里授权给人或部门</Radio>
            </Space>
          </Radio.Group>
        </Form.Item>
        <Form.Item name="doc_form" label="引用方式" extra="查到之后返回多大一块；上传文档后不能改">
          <Radio.Group>
            <Space direction="vertical" size={10}>
              {(["hierarchical_model", "text_model"] as DocForm[]).map((f) => (
                <Radio key={f} value={f}>
                  <b>
                    {DOC_FORM_LABEL[f]}
                    {f === "hierarchical_model" && "（推荐）"}
                  </b>
                  <div style={{ fontSize: 12.5, color: "var(--fg-2)" }}>{FORMS[f].desc}</div>
                  <div style={{ fontSize: 12, color: "var(--fg-3)" }}>例：{FORMS[f].eg}</div>
                  <div style={{ fontSize: 12, color: "var(--fg-4)" }}>{FORMS[f].tech}</div>
                </Radio>
              ))}
            </Space>
          </Radio.Group>
        </Form.Item>
      </Form>
    </Modal>
  );
}
