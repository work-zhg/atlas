"use client";

/**
 * API Key：两类 Key 都经模型网关调模型，平台管它们能用哪些模型、花多少钱。
 *
 *   个人 Key     一人可以多把（按用途分：Claude Code、脚本……），只能为本人新建；从公有模型和授权给
 *                自己（或所在部门）的私有模型里选。原文只在新建、重置时显示一次
 *   客户端 Key   应用调接口创建（Atlas 建智能体时：类型 Agent，编码 = 智能体 id），按「类型 + 编码」唯一；
 *                应用经内部接口取原文。平台服务的 Key（类型 Service）由管理员在这里事先建好
 *   限额         月预算（元，到了就拒绝）、每分钟次数、到期时间；都不填 = 不限
 *
 * ★ 管理员对个人 Key 只能改限额、停用（要写原因，本人看得到）、删除：模型由本人选，重置只能本人做。
 */
import { Alert, App as AntdApp, Button, Checkbox, Empty, Form, Input, InputNumber, Modal, Progress, Segmented, Select, Space, Table, Tag, Typography } from "antd";
import { useMemo, useState } from "react";

import {
  MG_CLIENT_TYPE,
  MG_KEY_MODEL_STATE,
  MG_KEY_STATUS,
  type MgKey,
  type MgKeyIn,
  type MgKeyWithSecret,
  type MgModel,
  useMgClientTypes,
  useMgKeyAdminActions,
  useMgKeys,
  useMgModels,
  useMgMyKeyActions,
  useMgMyKeys,
} from "@/api/model-gateway";
import { Icon } from "@/components/Icon";
import pageStyles from "@/features/agents/agents.module.css";
import styles from "@/features/catalog/catalog.module.css";
import { MgGate } from "./ModelsPage";
import { FORMAT_HINT, fmtTime, keyExpired, money, snippet, type SnippetKind } from "./shared";

function KeysPage({ title, action, children }: { title: string; action?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className={pageStyles.page}>
      <header className={pageStyles.head}>
        <div className={pageStyles.titleRow}>
          <h1 className={pageStyles.title}>{title}</h1>
          {action}
        </div>
      </header>
      <div className={pageStyles.body}>{children}</div>
    </div>
  );
}

/** 我的 Key：在 Claude Code、Cursor、脚本里用平台上的模型 */
export function MgUserKeyPage() {
  return <MgGate>{(me) => <MyKeys gatewayUrl={me.gateway_url} />}</MgGate>;
}

/** Key 管理（管理员）：全部个人 Key 和客户端 Key */
export function MgKeyAdminPage() {
  return (
    <MgGate>
      {(me) =>
        me.can_manage ? (
          <AllKeys />
        ) : (
          <KeysPage title="Key 管理">
            <Empty description="只有模型管理员能管理全部 Key" />
          </KeysPage>
        )
      }
    </MgGate>
  );
}

/** 能放进 Key 的模型：启用的（个人 Key 的列表后端已按权限过滤） */
const selectable = (models: MgModel[]) => models.filter((m) => m.enabled);

function KeyModels({ k }: { k: MgKey }) {
  if (!k.models.length) return <span style={{ color: "var(--warning)" }}>还没分配</span>;
  return (
    <span>
      {k.models.map((m) => (
        <Tag
          key={m.name}
          title={MG_KEY_MODEL_STATE[m.state] ? `${MG_KEY_MODEL_STATE[m.state]}，现在调不了` : m.name}
          style={{ textDecoration: m.state === "ok" ? undefined : "line-through", color: m.state === "ok" ? undefined : "var(--fg-4)" }}
        >
          {m.display_name || m.name}
        </Tag>
      ))}
    </span>
  );
}

function KeyStatus({ k }: { k: MgKey }) {
  if (k.status === "active" && keyExpired(k)) return <Tag color="error">已过期</Tag>;
  return (
    <span>
      <Tag color={MG_KEY_STATUS[k.status].color}>{MG_KEY_STATUS[k.status].label}</Tag>
      {k.status === "blocked" && k.block_reason && <div className={styles.desc2}>{k.block_reason}</div>}
    </span>
  );
}

function Usage({ k }: { k: MgKey }) {
  const spent = Number(k.spent_this_month);
  if (k.monthly_budget == null)
    return (
      <div>
        {money(spent)}
        <div className={styles.desc2}>不限预算</div>
      </div>
    );
  const budget = Number(k.monthly_budget);
  const pct = budget > 0 ? Math.min(100, Math.round((spent * 100) / budget)) : 100;
  return (
    <div>
      {money(spent)} <span className={styles.muted}>/ {money(budget)}</span>
      <Progress percent={pct} size="small" showInfo={false} status={pct >= 100 ? "exception" : pct >= 80 ? "active" : "normal"} />
    </div>
  );
}

function Limits({ k }: { k: MgKey }) {
  const parts = [k.rpm ? `${k.rpm} 次/分钟` : "", k.expires_at ? `${fmtTime(k.expires_at).slice(0, 10)} 到期` : ""].filter(Boolean);
  return parts.length ? <span>{parts.join(" · ")}</span> : <span className={styles.muted}>不限</span>;
}

// ═══════════════════════════════════════════════ 我的 Key

function MyKeys({ gatewayUrl }: { gatewayUrl: string | null }) {
  const { modal, message } = AntdApp.useApp();
  const keysQ = useMgMyKeys();
  const modelsQ = useMgModels();
  const { create, update, act, remove } = useMgMyKeyActions();
  const [editing, setEditing] = useState<{ key?: MgKey }>();
  const [secret, setSecret] = useState<{ title: string; key: MgKeyWithSecret }>();
  const keys = keysQ.data ?? [];
  const allowed = selectable(modelsQ.data ?? []);

  const reset = (k: MgKey) =>
    modal.confirm({
      title: `重置 Key ${k.label || k.masked || ""}`,
      okText: "重置",
      content: `生成新 Key，旧 Key ${k.masked ?? ""} 立即失效，用旧 Key 的工具要换成新的。模型和限额不变。`,
      onOk: () =>
        act.mutateAsync({ id: k.id, action: "reset" }).then(
          (r) => setSecret({ title: "已重置", key: r }),
          (e: Error) => message.error(e.message),
        ),
    });
  const block = (k: MgKey) =>
    modal.confirm({
      title: "停用 Key",
      okText: "停用",
      okButtonProps: { danger: true },
      content: "停用后用这把 Key 的调用都会失败，可以随时启用。",
      onOk: () =>
        act.mutateAsync({ id: k.id, action: "block" }).then(() => message.success("已停用"), (e: Error) => message.error(e.message)),
    });
  const del = (k: MgKey) =>
    modal.confirm({
      title: "删除 Key",
      okText: "删除",
      okButtonProps: { danger: true },
      content: `删除 ${k.label || k.masked || "这把 Key"}，不可恢复。只是临时不用，请选「停用」。`,
      onOk: () => remove.mutateAsync(k.id).then(() => message.success("已删除"), (e: Error) => message.error(e.message)),
    });

  return (
    <KeysPage
      title="我的 Key"
      action={
        <Button type="primary" className={pageStyles.action} icon={<Icon name="plus" size={14} />} onClick={() => setEditing({})}>
          新建 Key
        </Button>
      }
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="在 Claude Code、Cursor 或脚本里用平台上的模型。可以按用途建几把，各自设预算；授权收回后对应的模型随之失效。"
      />
      {keysQ.error && <Alert type="error" showIcon style={{ marginBottom: 12 }} message={keysQ.error.message} />}
      <Table<MgKey>
        size="middle"
        rowKey="id"
        loading={keysQ.isPending}
        dataSource={keys}
        pagination={false}
        locale={{ emptyText: <Empty description="还没有 Key" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
        columns={[
          {
            title: "Key",
            render: (_, k) => (
              <div>
                <b>{k.label || "未命名"}</b>
                <div className={styles.desc2} style={{ fontFamily: "var(--mono)" }}>
                  {k.masked}
                </div>
              </div>
            ),
          },
          { title: "模型", render: (_, k) => <KeyModels k={k} /> },
          { title: "本月花费", width: 160, render: (_, k) => <Usage k={k} /> },
          { title: "限额", width: 160, render: (_, k) => <Limits k={k} /> },
          { title: "状态", width: 120, render: (_, k) => <KeyStatus k={k} /> },
          { title: "最近使用", width: 140, render: (_, k) => fmtTime(k.last_used_at) },
          {
            title: "操作",
            width: 220,
            render: (_, k) => (
              <Space size={0}>
                <Button size="small" type="link" onClick={() => setEditing({ key: k })}>
                  编辑
                </Button>
                <Button size="small" type="link" onClick={() => reset(k)}>
                  重置
                </Button>
                {k.status === "blocked" ? (
                  <Button
                    size="small"
                    type="link"
                    onClick={() =>
                      act.mutate(
                        { id: k.id, action: "unblock" },
                        { onSuccess: () => message.success("已启用"), onError: (e) => message.error(e.message) },
                      )
                    }
                  >
                    启用
                  </Button>
                ) : (
                  <Button size="small" type="link" onClick={() => block(k)}>
                    停用
                  </Button>
                )}
                <Button size="small" type="link" danger onClick={() => del(k)}>
                  删除
                </Button>
              </Space>
            ),
          },
        ]}
      />
      {editing && (
        <KeyFormModal
          title={editing.key ? `编辑 Key · ${editing.key.label || editing.key.masked}` : "新建 Key"}
          okText={editing.key ? "保存" : "新建"}
          current={editing.key}
          allowed={allowed}
          withLabel
          withModels
          empty="还没有你能用的模型。请联系模型管理员开放公有模型，或给你授权私有模型。"
          loading={create.isPending || update.isPending}
          onClose={() => setEditing(undefined)}
          onSubmit={(body) =>
            editing.key
              ? update.mutate(
                  { id: editing.key.id, body },
                  {
                    onSuccess: () => {
                      message.success("已保存，Key 本身不变");
                      setEditing(undefined);
                    },
                    onError: (e) => message.error(e.message),
                  },
                )
              : create.mutate(body as MgKeyIn, {
                  onSuccess: (r) => {
                    setEditing(undefined);
                    setSecret({ title: "已新建 Key", key: r });
                  },
                  onError: (e) => message.error(e.message),
                })
          }
        />
      )}
      {secret && <SecretModal title={secret.title} k={secret.key} gatewayUrl={gatewayUrl} onClose={() => setSecret(undefined)} />}
    </KeysPage>
  );
}

function SecretModal({ title, k, gatewayUrl, onClose }: { title: string; k: MgKeyWithSecret; gatewayUrl: string | null; onClose: () => void }) {
  const [kind, setKind] = useState<SnippetKind>("claude-code");
  const model = k.models.find((m) => m.state === "ok")?.name ?? k.models[0]?.name ?? "<模型名>";
  if (!k.secret) return null;
  return (
    <Modal
      open
      width={640}
      title={title}
      onCancel={onClose}
      footer={
        <Button type="primary" onClick={onClose}>
          我已保存
        </Button>
      }
    >
      {/* antd 的 copyable 在 http 下会回落到 execCommand（112 不是 HTTPS，浏览器的剪贴板接口不可用） */}
      <Typography.Paragraph copyable={{ text: k.secret }} className={styles.code} style={{ marginBottom: 0 }}>
        {k.secret}
      </Typography.Paragraph>
      <Alert type="warning" showIcon style={{ marginTop: 12 }} message="Key 只显示这一次，关闭后无法再查看；忘记了可以「重置」。" />
      <div className={styles.secTitle}>怎么用</div>
      <Segmented
        value={kind}
        onChange={(v) => setKind(v as typeof kind)}
        style={{ marginBottom: 8 }}
        options={[
          { value: "claude-code", label: "Claude Code" },
          { value: "universal", label: "通用接口" },
          { value: "anthropic", label: "Anthropic 格式" },
          { value: "openai", label: "OpenAI 格式（Cursor）" },
        ]}
      />
      <pre className={styles.code}>{snippet(kind, gatewayUrl, model, k.secret)}</pre>
      <p className={styles.muted} style={{ marginTop: 8 }}>{FORMAT_HINT}</p>
    </Modal>
  );
}

// ═══════════════════════════════════════════════ Key 管理（管理员）

function AllKeys() {
  const { modal, message } = AntdApp.useApp();
  const [type, setType] = useState<"user" | "client">("client");
  const [clientType, setClientType] = useState<string>();
  const [status, setStatus] = useState<string>();
  const [q, setQ] = useState("");
  const keysQ = useMgKeys({ key_type: type, client_type: type === "client" ? clientType : undefined, status });
  const typesQ = useMgClientTypes();
  const modelsQ = useMgModels();
  const { createClient, update, reset, block, unblock, remove } = useMgKeyAdminActions();
  const [editing, setEditing] = useState<MgKey>();
  const [creating, setCreating] = useState(false);
  const [blocking, setBlocking] = useState<MgKey>();
  const allowed = selectable(modelsQ.data ?? []);
  const typeName = (t: string | null) => (t ? (MG_CLIENT_TYPE[t] ?? t) : "—");
  const title = (k: MgKey) =>
    k.key_type === "client" ? `${k.name || k.client_code}（${typeName(k.client_type)} · ${k.client_code}）` : `${k.name} · ${k.label || k.masked}`;

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (keysQ.data ?? []).filter(
      (k) =>
        !needle ||
        k.name.toLowerCase().includes(needle) ||
        k.label.toLowerCase().includes(needle) ||
        (k.client_code ?? "").toLowerCase().includes(needle),
    );
  }, [keysQ.data, q]);

  const confirmReset = (k: MgKey) =>
    modal.confirm({
      title: "重置客户端 Key",
      okText: "重置",
      content: `为「${title(k)}」生成新 Key。使用方会自动换用新的，旧 Key 还能用 10 分钟（给正在跑的调用留时间）。`,
      onOk: () => reset.mutateAsync(k.id).then(() => message.success("已重置"), (e: Error) => message.error(e.message)),
    });
  const del = (k: MgKey) =>
    modal.confirm({
      title: "删除 Key",
      okText: "删除",
      okButtonProps: { danger: true },
      content: `删除「${title(k)}」，不可恢复。${
        k.client_type === "Agent" ? "这个智能体之后不能运行。" : k.key_type === "user" ? "持有人要重新新建。" : "引用它的平台功能会停止。"
      }只是临时不让用，请选「停用」。`,
      onOk: () => remove.mutateAsync(k.id).then(() => message.success("已删除"), (e: Error) => message.error(e.message)),
    });

  return (
    <KeysPage
      title="Key 管理"
      action={
        type === "client" ? (
          <Button type="primary" className={pageStyles.action} icon={<Icon name="plus" size={14} />} onClick={() => setCreating(true)}>
            新建客户端 Key
          </Button>
        ) : undefined
      }
    >
      <div className={pageStyles.filterRow}>
        <Segmented
          value={type}
          onChange={(v) => setType(v as "user" | "client")}
          options={[
            { value: "client", label: "客户端 Key" },
            { value: "user", label: "个人 Key" },
          ]}
        />
        {type === "client" && (
          <Select
            allowClear
            placeholder="全部客户端类型"
            style={{ width: 170 }}
            value={clientType}
            onChange={setClientType}
            options={(typesQ.data ?? []).map((t) => ({ value: t.code, label: `${typeName(t.code)}（${t.code}）` }))}
          />
        )}
        <Select
          allowClear
          placeholder="全部状态"
          style={{ width: 130 }}
          value={status}
          onChange={setStatus}
          options={Object.entries(MG_KEY_STATUS).map(([value, s]) => ({ value, label: s.label }))}
        />
        <Input
          allowClear
          placeholder={type === "client" ? "搜索客户端编码 / 名称" : "搜索持有人 / 用途"}
          style={{ width: 220 }}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>
      <Table<MgKey>
        size="middle"
        rowKey="id"
        loading={keysQ.isPending}
        dataSource={rows}
        pagination={{ pageSize: 50, hideOnSinglePage: true }}
        locale={{ emptyText: <Empty description="没有符合条件的 Key" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
        columns={[
          {
            title: type === "client" ? "客户端" : "持有人 / 用途",
            render: (_, k) =>
              k.key_type === "client" ? (
                <div>
                  <b>{k.name || k.client_code}</b>
                  <div className={styles.desc2}>
                    {typeName(k.client_type)} · <code>{k.client_code}</code>
                  </div>
                </div>
              ) : (
                <div>
                  <b>{k.name}</b>
                  <div className={styles.desc2}>{k.label || "未命名"}</div>
                </div>
              ),
          },
          { title: "Key", width: 120, render: (_, k) => (k.masked ? <code>{k.masked}</code> : <span className={styles.muted}>—</span>) },
          { title: "模型", render: (_, k) => <KeyModels k={k} /> },
          { title: "本月花费", width: 150, render: (_, k) => <Usage k={k} /> },
          { title: "限额", width: 140, render: (_, k) => <Limits k={k} /> },
          { title: "状态", width: 130, render: (_, k) => <KeyStatus k={k} /> },
          {
            title: "操作",
            width: 230,
            render: (_, k) => (
              <Space size={0}>
                <Button size="small" type={k.status === "pending" ? "primary" : "link"} onClick={() => setEditing(k)}>
                  {k.key_type === "client" ? (k.status === "pending" ? "分配模型" : "编辑") : "改限额"}
                </Button>
                {k.key_type === "client" && k.status !== "pending" && (
                  <Button size="small" type="link" onClick={() => confirmReset(k)}>
                    重置
                  </Button>
                )}
                {k.status === "blocked" ? (
                  <Button
                    size="small"
                    type="link"
                    onClick={() => unblock.mutate(k.id, { onSuccess: () => message.success("已启用"), onError: (e) => message.error(e.message) })}
                  >
                    启用
                  </Button>
                ) : (
                  k.status !== "pending" && (
                    <Button size="small" type="link" onClick={() => setBlocking(k)}>
                      停用
                    </Button>
                  )
                )}
                <Button size="small" type="link" danger onClick={() => del(k)}>
                  删除
                </Button>
              </Space>
            ),
          },
        ]}
      />
      {editing && (
        <KeyFormModal
          title={`${editing.key_type === "client" ? "编辑" : "改限额"} · ${title(editing)}`}
          okText="保存"
          current={editing}
          allowed={allowed}
          withModels={editing.key_type === "client"}
          intro={editing.key_type === "user" ? "个人 Key 的模型由本人选；这里只能改限额。" : undefined}
          empty="还没有模型：先在「模型」里添加。"
          loading={update.isPending}
          onClose={() => setEditing(undefined)}
          onSubmit={(body) =>
            update.mutate(
              { id: editing.id, body },
              {
                onSuccess: () => {
                  message.success("已保存");
                  setEditing(undefined);
                },
                onError: (e) => message.error(e.message),
              },
            )
          }
        />
      )}
      {blocking && (
        <BlockModal
          k={blocking}
          title={title(blocking)}
          loading={block.isPending}
          onClose={() => setBlocking(undefined)}
          onSubmit={(reason) =>
            block.mutate(
              { id: blocking.id, reason },
              {
                onSuccess: () => {
                  message.success("已停用");
                  setBlocking(undefined);
                },
                onError: (e) => message.error(e.message),
              },
            )
          }
        />
      )}
      {creating && (
        <NewClientModal
          types={(typesQ.data ?? []).map((t) => t.code)}
          typeName={typeName}
          loading={createClient.isPending}
          onClose={() => setCreating(false)}
          onSubmit={(body) =>
            createClient.mutate(
              { ...body, label: "", models: [], monthly_budget: null, rpm: null, expires_at: null },
              {
                onSuccess: (k) => {
                  message.success(`已新建 ${k.client_type}:${k.client_code}，分配模型后就能用`);
                  setCreating(false);
                  setType("client");
                  setEditing(k);
                },
                onError: (e) => message.error(e.message),
              },
            )
          }
        />
      )}
    </KeysPage>
  );
}

function BlockModal({
  k,
  title,
  loading,
  onClose,
  onSubmit,
}: {
  k: MgKey;
  title: string;
  loading: boolean;
  onClose: () => void;
  onSubmit: (reason: string) => void;
}) {
  const [reason, setReason] = useState("");
  return (
    <Modal open title={`停用 · ${title}`} okText="停用" okButtonProps={{ danger: true, loading }} onCancel={onClose} onOk={() => onSubmit(reason.trim())}>
      <p>
        停用后用这把 Key 的调用都会失败{k.client_type === "Agent" ? "，这个智能体不能运行" : ""}。可以随时启用。
      </p>
      <Input.TextArea
        rows={2}
        maxLength={200}
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder={k.key_type === "user" ? "原因（持有人看得到，如：本月花费异常，请联系管理员）" : "原因"}
      />
    </Modal>
  );
}

function NewClientModal({
  types,
  typeName,
  loading,
  onClose,
  onSubmit,
}: {
  types: string[];
  typeName: (t: string) => string;
  loading: boolean;
  onClose: () => void;
  onSubmit: (body: { client_type: string; client_code: string; name: string }) => void;
}) {
  const [form] = Form.useForm<{ client_type: string; client_code: string; name: string }>();
  return (
    <Modal
      open
      title="新建客户端 Key"
      okText="新建"
      okButtonProps={{ loading }}
      onCancel={onClose}
      onOk={() =>
        form.validateFields().then((v) => onSubmit({ client_type: v.client_type, client_code: v.client_code.trim(), name: (v.name ?? "").trim() }))
      }
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="智能体的 Key 会自动创建。这里给平台功能（如会话标题、知识库）建 Key，建好后分配模型。"
      />
      <Form form={form} layout="vertical" initialValues={{ client_type: types.includes("Service") ? "Service" : types[0] }}>
        <Form.Item name="client_type" label="客户端类型" rules={[{ required: true }]}>
          <Select options={types.map((t) => ({ value: t, label: `${typeName(t)}（${t}）` }))} />
        </Form.Item>
        <Form.Item name="client_code" label="客户端编码" rules={[{ required: true, whitespace: true, message: "请填写客户端编码" }]}>
          <Input placeholder="如 session-title" maxLength={128} />
        </Form.Item>
        <Form.Item name="name" label="名称">
          <Input placeholder="如 会话标题" maxLength={200} />
        </Form.Item>
      </Form>
    </Modal>
  );
}

// ═══════════════════════════════════════════════ 新建 / 编辑：用途名、模型、限额

const DAY = 86_400_000;
const EXPIRY = [
  { value: "never", label: "永久" },
  { value: "30", label: "30 天后" },
  { value: "90", label: "90 天后" },
  { value: "180", label: "半年后" },
  { value: "365", label: "一年后" },
];

interface KeyValues {
  label?: string;
  models: string[];
  monthly_budget?: number | null;
  rpm?: number | null;
  expiry: string;
}

/**
 * 只有「指定模型」：从可选范围里逐个勾，新登记的模型不会自动进任何 Key。
 * 修改时只发改了的字段（后端：不传 = 不改，传 null = 清掉限额）。
 */
function KeyFormModal({
  title,
  okText,
  current,
  allowed,
  withLabel,
  withModels,
  intro,
  empty,
  loading,
  onClose,
  onSubmit,
}: {
  title: string;
  okText: string;
  current?: MgKey;
  allowed: MgModel[];
  withLabel?: boolean;
  withModels?: boolean;
  intro?: string;
  empty: string;
  loading: boolean;
  onClose: () => void;
  onSubmit: (body: Partial<MgKeyIn>) => void;
}) {
  const [form] = Form.useForm<KeyValues>();
  const names = allowed.map((m) => m.name);
  const assigned = current?.models ?? [];
  // 分配过、但已不在可选范围里的：保存时移除
  const stale = assigned.filter((m) => !names.includes(m.name));
  const keepExpiry = current?.expires_at ? `keep` : "never";
  const expiryOptions = current?.expires_at
    ? [{ value: "keep", label: `不变（${fmtTime(current.expires_at).slice(0, 10)} 到期）` }, ...EXPIRY]
    : EXPIRY;

  const submit = async () => {
    const v = await form.validateFields();
    const expires_at = v.expiry === "never" ? null : v.expiry === "keep" ? current?.expires_at ?? null : new Date(Date.now() + Number(v.expiry) * DAY).toISOString();
    const body: Partial<MgKeyIn> = { monthly_budget: v.monthly_budget ?? null, rpm: v.rpm ?? null, expires_at };
    if (withLabel) body.label = v.label?.trim() ?? "";
    if (withModels) body.models = v.models;
    onSubmit(body);
  };

  return (
    <Modal open width={600} title={title} okText={okText} okButtonProps={{ loading, disabled: withModels && !allowed.length }} onCancel={onClose} onOk={submit}>
      {intro && <Alert type="info" showIcon style={{ marginBottom: 12 }} message={intro} />}
      <Form
        form={form}
        layout="vertical"
        initialValues={{
          label: current?.label ?? "",
          models: assigned.map((m) => m.name).filter((n) => names.includes(n)),
          monthly_budget: current?.monthly_budget == null ? null : Number(current.monthly_budget),
          rpm: current?.rpm ?? null,
          expiry: keepExpiry,
        }}
      >
        {withLabel && (
          <Form.Item name="label" label="用途" extra="给自己看的，比如 Claude Code、数据脚本">
            <Input maxLength={100} placeholder="如 Claude Code" />
          </Form.Item>
        )}
        {withModels &&
          (allowed.length ? (
            <Form.Item
              name="models"
              label="模型"
              rules={[{ validator: (_, v: string[]) => (v?.length ? Promise.resolve() : Promise.reject(new Error("至少选一个模型"))) }]}
              extra="以后新增的模型不会自动加入"
            >
              <Checkbox.Group style={{ display: "grid", gap: 6, maxHeight: 260, overflowY: "auto" }}>
                {allowed.map((m) => (
                  <Checkbox key={m.name} value={m.name}>
                    {m.display_name} <span className={styles.muted} style={{ fontFamily: "var(--mono)" }}>{m.name}</span>{" "}
                    {m.visibility === "public" ? <Tag color="blue">公有</Tag> : <Tag>私有</Tag>}
                  </Checkbox>
                ))}
              </Checkbox.Group>
            </Form.Item>
          ) : (
            <Alert type="warning" showIcon style={{ marginBottom: 12 }} message={empty} />
          ))}
        {withModels &&
          stale.map((m) => (
            <div key={m.name} className={styles.muted} style={{ marginBottom: 6 }}>
              <code>{m.name}</code> <span style={{ color: "var(--warning)" }}>{MG_KEY_MODEL_STATE[m.state] || "不在可选范围"}，保存时移除</span>
            </div>
          ))}
        <Space.Compact block style={{ gap: 12 }}>
          <Form.Item name="monthly_budget" label="月预算（元）" style={{ flex: 1 }} extra="到了就拒绝调用；不填 = 不限">
            <InputNumber min={0} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item name="rpm" label="每分钟最多（次）" style={{ flex: 1 }} extra="不填 = 不限">
            <InputNumber min={1} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item name="expiry" label="到期" style={{ flex: 1 }}>
            <Select options={expiryOptions} />
          </Form.Item>
        </Space.Compact>
      </Form>
    </Modal>
  );
}
