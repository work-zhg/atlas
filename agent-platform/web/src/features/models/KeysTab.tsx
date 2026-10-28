"use client";

/**
 * API Key：两类 Key 都直接调 LiteLLM，平台只管它们能用哪些模型。
 *
 *   用户 Key     每人一把，只能为本人申请；从公有模型和授权给自己（或所在部门）的私有模型里逐个选。
 *                原文只在申请、重置时显示一次
 *   客户端 Key   应用调接口创建（Atlas 建智能体时：类型 Agent，编码 = 智能体 id），按「类型 + 编码」唯一；
 *                管理员分配模型后才在 LiteLLM 生成 Key，应用经接口取。平台服务的 Key（类型 Service）
 *                由管理员在这里事先建好
 *
 * ★ 只有「指定模型」：新登记的模型不会自动进任何 Key。
 */
import { Alert, App as AntdApp, Button, Checkbox, Empty, Form, Input, Modal, Segmented, Select, Space, Table, Tag, Typography } from "antd";
import { useMemo, useState } from "react";

import {
  MG_CLIENT_TYPE,
  MG_KEY_MODEL_STATE,
  MG_KEY_STATUS,
  type MgKey,
  type MgModel,
  useMgClientKeyActions,
  useMgClientKeys,
  useMgClientTypes,
  useMgModels,
  useMgMyKey,
  useMgMyKeyActions,
} from "@/api/model-gateway";
import { Icon } from "@/components/Icon";
import pageStyles from "@/features/agents/agents.module.css";
import styles from "@/features/catalog/catalog.module.css";
import { useMe } from "@/lib/session";
import { curlFor } from "./shared";

export function KeysTab({ manage, litellmUrl }: { manage: boolean; litellmUrl: string | null }) {
  const [tab, setTab] = useState<"user" | "client">("user");
  const clientsQ = useMgClientKeys(manage);
  const clients = clientsQ.data ?? [];
  const pending = clients.filter((k) => k.status === "pending").length;

  return (
    <>
      {manage && (
        <Segmented
          style={{ marginBottom: 12 }}
          value={tab}
          onChange={(v) => setTab(v as "user" | "client")}
          options={[
            { value: "user", label: "我的用户 Key" },
            {
              value: "client",
              label: (
                <span>
                  客户端 Key {clients.length > 0 && <span className={styles.muted}>{clients.length}</span>}
                  {pending > 0 && (
                    <Tag color="warning" style={{ marginLeft: 6, marginRight: 0 }}>
                      待分配 {pending}
                    </Tag>
                  )}
                </span>
              ),
            },
          ]}
        />
      )}
      {tab === "client" && manage ? <ClientKeys keys={clients} loading={clientsQ.isPending} /> : <UserKey manage={manage} litellmUrl={litellmUrl} />}
    </>
  );
}

/** 能放进 Key 的模型：平台登记的、LiteLLM 里还在的。用户 Key 的列表后端已按权限过滤（管理员是全部） */
function selectable(models: MgModel[]): MgModel[] {
  return models.filter((m) => m.source === "platform" && m.status !== "missing");
}

function KeyModels({ k }: { k: MgKey }) {
  if (k.status === "pending") return <span className={styles.muted}>—</span>;
  return (
    <span>
      {k.models.map((m) => (
        <Tag
          key={m.name}
          color={m.state === "ok" ? undefined : m.state === "missing" ? "warning" : "default"}
          title={MG_KEY_MODEL_STATE[m.state] || undefined}
          style={{
            fontFamily: "var(--mono)",
            textDecoration: m.state === "revoked" || m.state === "deleted" ? "line-through" : undefined,
          }}
        >
          {m.name}
        </Tag>
      ))}
      {k.no_models && (
        <Tag color="error" title="分配的模型都不可用了：LiteLLM 里这把 Key 已停用">
          无可用模型
        </Tag>
      )}
    </span>
  );
}

function KeyStatus({ k }: { k: MgKey }) {
  return (
    <span>
      <Tag color={MG_KEY_STATUS[k.status].color}>{MG_KEY_STATUS[k.status].label}</Tag>
      {k.sync_error && (
        <Tag color="error" title={k.sync_error}>
          同步 LiteLLM 失败
        </Tag>
      )}
    </span>
  );
}

const fmtTime = (at: string | null) => (at ? at.slice(0, 16).replace("T", " ") : "—");

// ═══════════════════════════════════════════════ 我的用户 Key

function UserKey({ manage, litellmUrl }: { manage: boolean; litellmUrl: string | null }) {
  const { modal, message } = AntdApp.useApp();
  const meQ = useMe();
  const keyQ = useMgMyKey();
  const modelsQ = useMgModels();
  const { apply, assign, act, remove } = useMgMyKeyActions();
  const [picking, setPicking] = useState(false);
  const [secret, setSecret] = useState<{ title: string; value: string; model?: string }>();
  const k = keyQ.data ?? null;
  const allowed = selectable(modelsQ.data?.data ?? []);

  const showSecret = (title: string, r: { secret: string | null; effective: string[] }) =>
    r.secret && setSecret({ title, value: r.secret, model: r.effective[0] });

  const reset = () =>
    modal.confirm({
      title: "重置用户 Key",
      okText: "重置",
      content: `生成新 Key，旧 Key ${k?.masked ?? ""} 立即失效，用旧 Key 的工具要换成新 Key。分配的模型、停用状态保持不变。`,
      onOk: () =>
        act.mutateAsync("reset").then(
          (r) => showSecret("已重置用户 Key", r),
          (e: Error) => message.error(e.message),
        ),
    });
  const block = () =>
    modal.confirm({
      title: "停用用户 Key",
      okText: "停用",
      okButtonProps: { danger: true },
      content: "停用后 LiteLLM 拒绝这把 Key 的所有调用，可以随时启用。",
      onOk: () => act.mutateAsync("block").then(() => message.success("已停用"), (e: Error) => message.error(e.message)),
    });
  const del = () =>
    modal.confirm({
      title: "删除用户 Key",
      okText: "删除",
      okButtonProps: { danger: true },
      content: `删除你的用户 Key ${k?.masked ?? ""}，不可恢复。删除后要重新申请；只是临时不用，请选「停用」。`,
      onOk: () => remove.mutateAsync().then(() => message.success("已删除"), (e: Error) => message.error(e.message)),
    });

  return (
    <>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message={`每人一把用户 Key，只能为本人申请，用在 Claude Code、Cursor、脚本等个人工具里，直接调用 LiteLLM${litellmUrl ? `（${litellmUrl}）` : ""}。模型从${manage ? "全部已登记模型" : "公有模型和授权给你或你所在部门的私有模型"}里逐个选；私有模型的授权撤销后，Key 里的这个模型随之失效。`}
      />
      {keyQ.error && <Alert type="error" showIcon style={{ marginBottom: 12 }} message={keyQ.error.message} />}
      <Table
        size="middle"
        rowKey={() => "me"}
        loading={keyQ.isPending}
        dataSource={[{ k }]}
        pagination={false}
        columns={[
          {
            title: "本人",
            render: () => (
              <div>
                <b>{meQ.data?.name ?? "我"}</b>
                {meQ.data?.account && <div className={styles.desc2}>{meQ.data.account}</div>}
              </div>
            ),
          },
          { title: "Key", width: 130, render: () => (k ? <code>{k.masked}</code> : <span className={styles.muted}>未申请</span>) },
          { title: "模型", render: () => (k ? <KeyModels k={k} /> : "—") },
          { title: "状态", width: 150, render: () => (k ? <KeyStatus k={k} /> : "—") },
          { title: "申请时间", width: 150, render: () => fmtTime(k?.created_at ?? null) },
          {
            title: "操作",
            width: 240,
            render: () =>
              k ? (
                <Space size={0}>
                  <Button size="small" type="link" onClick={() => setPicking(true)}>
                    分配模型
                  </Button>
                  <Button size="small" type="link" onClick={reset}>
                    重置
                  </Button>
                  {k.status === "blocked" ? (
                    <Button
                      size="small"
                      type="link"
                      onClick={() =>
                        act.mutate("unblock", {
                          onSuccess: () => message.success("已启用"),
                          onError: (e) => message.error(e.message),
                        })
                      }
                    >
                      启用
                    </Button>
                  ) : (
                    <Button size="small" type="link" onClick={block}>
                      停用
                    </Button>
                  )}
                  <Button size="small" type="link" danger onClick={del}>
                    删除
                  </Button>
                </Space>
              ) : (
                <Button size="small" type="primary" disabled={keyQ.isPending} onClick={() => setPicking(true)}>
                  申请用户 Key
                </Button>
              ),
          },
        ]}
      />
      {picking && (
        <AssignModal
          title={k ? "分配模型 · 我的用户 Key" : "申请用户 Key"}
          okText={k ? "保存" : "申请"}
          allowed={allowed}
          current={k}
          hint={`${manage ? "管理员可选全部已登记模型" : "可选公有模型和授权给你或你所在部门的私有模型"}；以后登记的新模型不会自动加入`}
          empty="还没有你能用的模型。请联系管理员登记公有模型，或给你授权私有模型。"
          loading={apply.isPending || assign.isPending}
          onClose={() => setPicking(false)}
          onSubmit={(models) =>
            k
              ? assign.mutate(models, {
                  onSuccess: () => {
                    message.success("已更新模型，Key 本身不变");
                    setPicking(false);
                  },
                  onError: (e) => message.error(e.message),
                })
              : apply.mutate(models, {
                  onSuccess: (r) => {
                    setPicking(false);
                    showSecret("已申请用户 Key", r);
                  },
                  onError: (e) => message.error(e.message),
                })
          }
        />
      )}
      {secret && (
        <Modal
          open
          title={secret.title}
          onCancel={() => setSecret(undefined)}
          footer={
            <Button type="primary" onClick={() => setSecret(undefined)}>
              我已保存
            </Button>
          }
        >
          {/* antd 的 copyable 在 http 下会回落到 execCommand（112 不是 HTTPS，浏览器的剪贴板接口不可用） */}
          <Typography.Paragraph
            copyable={{ text: secret.value }}
            className={styles.code}
            style={{ marginBottom: 0 }}
          >
            {secret.value}
          </Typography.Paragraph>
          <Alert type="warning" showIcon style={{ marginTop: 12 }} message="Key 只显示这一次，关闭后无法再查看；忘记了可以「重置」。" />
          <div className={styles.secTitle}>怎么用</div>
          <p className={styles.muted}>
            在 Claude Code、Cursor 或脚本里把 Base URL 设为 LiteLLM 的地址
            {litellmUrl ? `（${litellmUrl}）` : "（请咨询管理员）"}，API Key 填这把 Key。
          </p>
          <pre className={styles.code}>{curlFor(litellmUrl, secret.model ?? "deepseek-chat", secret.value)}</pre>
        </Modal>
      )}
    </>
  );
}

// ═══════════════════════════════════════════════ 客户端 Key（管理员）

function ClientKeys({ keys, loading }: { keys: MgKey[]; loading: boolean }) {
  const { modal, message } = AntdApp.useApp();
  const typesQ = useMgClientTypes(true);
  const modelsQ = useMgModels();
  const { create, assign, act, remove } = useMgClientKeyActions();
  const [type, setType] = useState<string>();
  const [status, setStatus] = useState<string>();
  const [q, setQ] = useState("");
  const [picking, setPicking] = useState<MgKey>();
  const [creating, setCreating] = useState(false);
  const allowed = selectable(modelsQ.data?.data ?? []);
  const appsOf = (t: string | null) => typesQ.data?.find((x) => x.code === t)?.apps.join("、") ?? "";
  const typeName = (t: string | null) => (t ? (MG_CLIENT_TYPE[t] ?? t) : "—");
  const title = (k: MgKey) => `${k.name || k.client_code}（${typeName(k.client_type)} · ${k.client_code}）`;

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return keys.filter(
      (k) =>
        (!type || k.client_type === type) &&
        (!status || k.status === status) &&
        (!needle || (k.client_code ?? "").toLowerCase().includes(needle) || k.name.toLowerCase().includes(needle)),
    );
  }, [keys, type, status, q]);

  const reset = (k: MgKey) =>
    modal.confirm({
      title: "重置客户端 Key",
      okText: "重置",
      content: `为「${title(k)}」生成新 Key，旧 Key ${k.masked ?? ""} 立即失效。正在用旧 Key 的调用会失败，应用下一次获取时拿到新的。分配的模型、停用状态保持不变。`,
      onOk: () =>
        act.mutateAsync({ id: k.uuid, action: "reset" }).then(
          () => message.success("已重置"),
          (e: Error) => message.error(e.message),
        ),
    });
  const block = (k: MgKey) =>
    modal.confirm({
      title: "停用客户端 Key",
      okText: "停用",
      okButtonProps: { danger: true },
      content: `停用后 LiteLLM 拒绝「${title(k)}」这把 Key 的所有调用${k.client_type === "Agent" ? "，这个智能体在 Atlas 里不能运行" : ""}。可以随时启用。`,
      onOk: () =>
        act.mutateAsync({ id: k.uuid, action: "block" }).then(
          () => message.success("已停用"),
          (e: Error) => message.error(e.message),
        ),
    });
  const del = (k: MgKey) =>
    modal.confirm({
      title: "删除客户端 Key",
      okText: "删除",
      okButtonProps: { danger: true },
      content: `删除「${title(k)}」的客户端 Key${k.masked ? `（${k.masked}）` : ""}，LiteLLM 里的 Key 一并删除。${
        k.client_type === "Agent"
          ? "这个智能体之后不能运行；Atlas 下次用到它时会重新建一把「待分配」的 Key。"
          : "引用它的平台功能会停止，直到重新建好并分配模型。"
      }只是临时不让用，请选「停用」。`,
      onOk: () => remove.mutateAsync(k.uuid).then(() => message.success("已删除"), (e: Error) => message.error(e.message)),
    });

  return (
    <>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="客户端 Key 由应用调接口创建：Atlas 新建智能体时创建一条（客户端类型 Agent，客户端编码 = 智能体 id）。新建的 Key「待分配模型」，分配模型后才在 LiteLLM 生成 Key，应用按客户端类型 + 客户端编码获取。会话标题、记忆补记、知识库这类平台服务的 Key（类型 Service）由管理员在这里事先建好、分配模型，再在 Atlas、知识库的配置里引用。"
      />
      <div className={pageStyles.filterRow}>
        <Select
          allowClear
          placeholder="全部客户端类型"
          style={{ width: 170 }}
          value={type}
          onChange={setType}
          options={(typesQ.data ?? []).map((t) => ({ value: t.code, label: `${typeName(t.code)}（${t.code}）` }))}
        />
        <Select
          allowClear
          placeholder="全部状态"
          style={{ width: 130 }}
          value={status}
          onChange={setStatus}
          options={Object.entries(MG_KEY_STATUS).map(([value, s]) => ({ value, label: s.label }))}
        />
        <Input allowClear placeholder="搜索客户端编码 / 名称" style={{ width: 220 }} value={q} onChange={(e) => setQ(e.target.value)} />
        <Button
          type="primary"
          style={{ marginLeft: "auto" }}
          icon={<Icon name="plus" size={14} />}
          onClick={() => setCreating(true)}
        >
          新建客户端 Key
        </Button>
      </div>
      <Table<MgKey>
        size="middle"
        rowKey="uuid"
        loading={loading}
        dataSource={rows}
        pagination={false}
        locale={{ emptyText: <Empty description="没有符合条件的客户端 Key" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
        columns={[
          {
            title: "客户端类型",
            width: 140,
            render: (_, k) => (
              <span>
                {typeName(k.client_type)} <span className={styles.muted}>{k.client_type}</span>
              </span>
            ),
          },
          {
            title: "客户端编码 / 名称",
            render: (_, k) => (
              <div>
                <code>{k.client_code}</code>
                <div className={styles.desc2}>{k.name}</div>
              </div>
            ),
          },
          {
            title: "Key",
            width: 130,
            render: (_, k) => (k.masked ? <code>{k.masked}</code> : <span className={styles.muted}>分配模型后生成</span>),
          },
          { title: "模型", render: (_, k) => <KeyModels k={k} /> },
          { title: "状态", width: 150, render: (_, k) => <KeyStatus k={k} /> },
          {
            title: "创建",
            width: 150,
            render: (_, k) => (
              <div>
                {appsOf(k.client_type) || "—"}
                <div className={styles.desc2}>{fmtTime(k.created_at)}</div>
              </div>
            ),
          },
          {
            title: "操作",
            width: 240,
            render: (_, k) =>
              k.status === "pending" ? (
                <Space size={0}>
                  <Button size="small" type="primary" onClick={() => setPicking(k)}>
                    分配模型
                  </Button>
                  <Button size="small" type="link" danger onClick={() => del(k)}>
                    删除
                  </Button>
                </Space>
              ) : (
                <Space size={0}>
                  <Button size="small" type="link" onClick={() => setPicking(k)}>
                    分配模型
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
                          { id: k.uuid, action: "unblock" },
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
      {picking && (
        <AssignModal
          title={`分配模型 · ${title(picking)}`}
          okText={picking.status === "pending" ? "分配并生成 Key" : "保存"}
          allowed={allowed}
          current={picking}
          intro={
            picking.status === "pending"
              ? `这是${appsOf(picking.client_type) || "应用"}新建的客户端 Key。分配模型后才在 LiteLLM 生成 Key，应用随后就能取到并调用。`
              : undefined
          }
          hint={`客户端 Key 可分配全部已登记模型，公有、私有都行；以后登记的新模型不会自动加入${
            picking.status === "pending" ? "" : "。修改立即生效，Key 本身不变"
          }${picking.client_type === "Agent" ? "；Atlas 编辑这个智能体时，可选模型随之变化" : ""}`}
          empty="还没有已登记的模型：先在「模型」里登记。"
          loading={assign.isPending}
          onClose={() => setPicking(undefined)}
          onSubmit={(models) =>
            assign.mutate(
              { id: picking.uuid, models },
              {
                onSuccess: () => {
                  message.success(picking.status === "pending" ? "已分配模型并生成 Key" : "已更新模型");
                  setPicking(undefined);
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
          loading={create.isPending}
          onClose={() => setCreating(false)}
          onSubmit={(body) =>
            create.mutate(body, {
              onSuccess: (k) => {
                message.success(`已新建客户端 Key ${k.client_type}:${k.client_code}（待分配模型）`);
                setCreating(false);
                setPicking(k);
              },
              onError: (e) => message.error(e.message),
            })
          }
        />
      )}
    </>
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
        form
          .validateFields()
          .then((v) => onSubmit({ client_type: v.client_type, client_code: v.client_code.trim(), name: (v.name ?? "").trim() }))
      }
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="智能体的 Key 由 Atlas 自动创建。这里用来事先建平台服务这类 Key（如会话标题），建好后分配模型，再在 Atlas、知识库的配置里引用它。"
      />
      <Form form={form} layout="vertical" initialValues={{ client_type: types.includes("Service") ? "Service" : types[0] }}>
        <Form.Item name="client_type" label="客户端类型" rules={[{ required: true }]}>
          <Select options={types.map((t) => ({ value: t, label: `${typeName(t)}（${t}）` }))} />
        </Form.Item>
        <Form.Item
          name="client_code"
          label="客户端编码"
          rules={[{ required: true, whitespace: true, message: "请填写客户端编码" }]}
        >
          <Input placeholder="如 session-title" maxLength={128} />
        </Form.Item>
        <Form.Item name="name" label="名称">
          <Input placeholder="如 会话标题" maxLength={200} />
        </Form.Item>
      </Form>
    </Modal>
  );
}

/** 分配模型：只有「指定模型」，从可选范围里逐个勾；全选只勾当前已有的 */
function AssignModal({
  title,
  okText,
  allowed,
  current,
  intro,
  hint,
  empty,
  loading,
  onClose,
  onSubmit,
}: {
  title: string;
  okText: string;
  allowed: MgModel[];
  current: MgKey | null;
  intro?: string;
  hint: string;
  empty: string;
  loading: boolean;
  onClose: () => void;
  onSubmit: (models: string[]) => void;
}) {
  const names = allowed.map((m) => m.name);
  const assigned = current?.models ?? [];
  const [picked, setPicked] = useState<string[]>(assigned.map((m) => m.name).filter((n) => names.includes(n)));
  // 分配过、但已不在可选范围里的：保存时移除
  const stale = assigned.filter((m) => !names.includes(m.name));
  const [error, setError] = useState(false);

  return (
    <Modal
      open
      width={560}
      title={title}
      okText={okText}
      okButtonProps={{ loading, disabled: !allowed.length }}
      onCancel={onClose}
      onOk={() => {
        if (!picked.length) {
          setError(true);
          return;
        }
        onSubmit(picked);
      }}
    >
      {intro && <Alert type="info" showIcon style={{ marginBottom: 12 }} message={intro} />}
      {allowed.length ? (
        <>
          <div style={{ display: "flex", alignItems: "center", marginBottom: 8 }} className={styles.muted}>
            已选 {picked.length} / {allowed.length}
            <span style={{ marginLeft: "auto" }}>
              <Button size="small" type="link" onClick={() => setPicked(names)}>
                全选
              </Button>
              <Button size="small" type="link" onClick={() => setPicked([])}>
                清空
              </Button>
            </span>
          </div>
          <Checkbox.Group
            style={{ display: "grid", gap: 6, maxHeight: 320, overflowY: "auto" }}
            value={picked}
            onChange={(v) => {
              setPicked(v as string[]);
              setError(false);
            }}
          >
            {allowed.map((m) => (
              <Checkbox key={m.name} value={m.name}>
                {m.display_name} <span className={styles.muted} style={{ fontFamily: "var(--mono)" }}>{m.name}</span>{" "}
                {m.visibility === "public" ? <Tag color="blue">公有</Tag> : <Tag>私有</Tag>}
              </Checkbox>
            ))}
          </Checkbox.Group>
          {stale.map((m) => (
            <div key={m.name} className={styles.muted} style={{ marginTop: 6 }}>
              <Checkbox checked disabled /> <code>{m.name}</code>{" "}
              <span style={{ color: "var(--warning)" }}>{MG_KEY_MODEL_STATE[m.state] || "不在可选范围"}，保存时移除</span>
            </div>
          ))}
          {error && <div style={{ color: "var(--danger)", marginTop: 8 }}>至少选择一个模型</div>}
          <p className={styles.muted} style={{ marginTop: 12 }}>
            {hint}
          </p>
        </>
      ) : (
        <Alert type="warning" showIcon message={empty} />
      )}
    </Modal>
  );
}
