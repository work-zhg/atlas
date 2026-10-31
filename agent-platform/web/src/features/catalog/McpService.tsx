"use client";

/**
 * 一个 MCP 服务的详情：工具、授权、连接与凭据、被引用。
 *
 *   工具        MCP 代理的从上游拉取；REST、gRPC 的由网关维护（REST 能改映射，gRPC 只能改描述和副作用）。
 *               停用的工具对所有 Key 都不可用，和授权无关。试调和真实调用走同一条路（认 Key、判权、调上游）
 *   授权        见 McpGrants
 *   连接与凭据  只有 MCP 管理员能改；上游凭据不回显
 */
import {
  Alert,
  App as AntdApp,
  Button,
  Empty,
  Input,
  InputNumber,
  Modal,
  Radio,
  Select,
  Skeleton,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import Link from "next/link";
import { useMemo, useState } from "react";

import { useMcpServer } from "@/api/catalog";
import {
  GW_EFFECT,
  type GwCredentialIn,
  type GwDiscovered,
  type GwEffect,
  type GwKey,
  type GwMe,
  type GwRestMapping,
  type GwRestParam,
  type GwServiceDetail,
  type GwTool,
  type GwTryOut,
  type GwVisibility,
  useGwKeys,
  useGwMyKey,
  useGwService,
  useGwServiceActions,
  useGwToolActions,
} from "@/api/mcp-gateway";
import { Icon } from "@/components/Icon";
import { relativeTime } from "@/lib/format";
import styles from "./catalog.module.css";
import {
  type Connection,
  ConnectionFields,
  CredentialFields,
  DescriptorUpload,
  DiscoverError,
  DiscoveredTable,
  EffectTag,
  FormField,
  IdentityCheckbox,
  KindTag,
  STREAM_LABEL,
  VisibilityTag,
  connectionBody,
  connectionOf,
  connectionRows,
  credentialBody,
  credentialLabel,
  credentialOf,
  defaultPicked,
  fmtTime,
} from "./McpForms";
import { ServiceGrants } from "./McpGrants";

const json = (v: unknown) => JSON.stringify(v, null, 2);

export function ServiceDetail({ name, me, onClose }: { name: string; me: GwMe; onClose: () => void }) {
  const { modal, message } = AntdApp.useApp();
  const detailQ = useGwService(name);
  const actions = useGwServiceActions(name);
  const [editing, setEditing] = useState(false);
  const [tab, setTab] = useState("tools");
  const d = detailQ.data;

  if (!d) {
    return detailQ.error ? (
      <Alert type="error" showIcon message={detailQ.error.message} />
    ) : (
      <Skeleton active paragraph={{ rows: 10 }} />
    );
  }

  const toggle = () =>
    d.enabled
      ? modal.confirm({
          title: `停用「${d.display_name}」`,
          okText: "停用",
          okButtonProps: { danger: true },
          content:
            "停用后经网关的调用都会被拒绝（service_disabled），Atlas 编辑器里也选不到。授权和工具定义保留，重新启用后恢复。服务不能删除：服务名是工具名前缀、用户中心里的数据标识、智能体配置里的引用。",
          onOk: () =>
            actions.setEnabled.mutateAsync(false).then(
              () => message.success("已停用"),
              (e: Error) => message.error(e.message),
            ),
        })
      : actions.setEnabled.mutate(true, {
          onSuccess: () => message.success("已启用"),
          onError: (e) => message.error(e.message),
        });

  const endpoint = me.public_url ? `${me.public_url}/mcp/${d.name}` : null;
  const items = [
    { key: "tools", label: `工具 ${d.tools.length}`, children: <ToolsTab service={d} me={me} /> },
    ...(d.can_assign
      ? [{ key: "grants", label: d.visibility === "public" ? "所有者" : "授权", children: <ServiceGrants service={d} /> }]
      : []),
    ...(d.can_assign || d.can_manage ? [{ key: "conn", label: "连接与凭据", children: <ConnTab service={d} /> }] : []),
    { key: "refs", label: "被引用", children: <RefsTab name={d.name} /> },
  ];

  return (
    <>
      <div className={styles.detailHead}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 className={styles.detailTitle}>
            {d.display_name} <span className={styles.sub}>{d.name}</span>
          </h2>
          <div style={{ marginTop: 6 }}>
            <KindTag kind={d.kind} />
            <VisibilityTag visibility={d.visibility} />
            {d.enabled ? <Tag color="success">启用</Tag> : <Tag>已停用</Tag>}
          </div>
        </div>
        {d.can_manage && (
          <Space>
            <Button size="small" onClick={() => setEditing(true)}>
              编辑
            </Button>
            <Button size="small" danger={d.enabled} loading={actions.setEnabled.isPending} onClick={toggle}>
              {d.enabled ? "停用" : "启用"}
            </Button>
          </Space>
        )}
        <Button size="small" type="text" icon={<Icon name="x" size={14} />} onClick={onClose} />
      </div>
      {d.description && <p className={styles.muted}>{d.description}</p>}
      <div className={styles.muted} style={{ fontSize: 12 }}>
        外部客户端地址：
        {endpoint ? (
          <Typography.Text copyable={{ text: endpoint }} code>
            {endpoint}
          </Typography.Text>
        ) : (
          "MCP 网关还没对集群外开放（请咨询管理员）"
        )}
        {" · "}登记于 {fmtTime(d.created_at)}
      </div>
      <Tabs style={{ marginTop: 8 }} activeKey={tab} onChange={setTab} items={items} />
      {editing && <EditServiceModal service={d} onClose={() => setEditing(false)} />}
    </>
  );
}

// ═══════════════════════════════════════════════ 工具

function mappingCell(s: GwServiceDetail, t: GwTool) {
  const m = t.mapping ?? {};
  if (s.kind === "rest") return <code>{`${String(m.method ?? "")} ${String(m.path ?? "")}`}</code>;
  if (s.kind === "grpc")
    return (
      <span>
        <code>{String(m.method ?? "")}</code>
        {m.stream && m.stream !== "unary" ? <Tag style={{ marginLeft: 4 }}>{STREAM_LABEL[String(m.stream)]}</Tag> : null}
      </span>
    );
  return <span className={styles.muted}>上游同名工具</span>;
}

function ToolStatus({ t }: { t: GwTool }) {
  if (t.status === "disabled") return <Tag>已停用</Tag>;
  if (t.issues.length)
    return (
      <Tooltip title={t.issues.join("；")}>
        <Tag color="error">定义不合规</Tag>
      </Tooltip>
    );
  return <Tag color="success">可用</Tag>;
}

/** 当前用户的用户 Key 能不能用这个工具 */
function myKeyCell(s: GwServiceDetail, t: GwTool) {
  switch (s.my.scope) {
    case "public":
      return "✓ 公开";
    case "all":
      return "✓ 服务级";
    case "tools":
      return s.my.tools.includes(t.name) ? "✓ 工具级" : <span className={styles.muted}>没有授权</span>;
    case "no_key":
      return <span className={styles.muted}>还没有 Key</span>;
    default:
      return <span className={styles.muted}>没有授权</span>;
  }
}

function ToolsTab({ service: s, me }: { service: GwServiceDetail; me: GwMe }) {
  const { modal, message } = AntdApp.useApp();
  const actions = useGwServiceActions(s.name);
  const tools = useGwToolActions(s.name);
  const [viewing, setViewing] = useState<GwTool | "new">();
  const [trying, setTrying] = useState<GwTool>();
  const [rediscovering, setRediscovering] = useState(false);
  const manage = s.can_manage;
  const specUrl = typeof s.connection.spec_url === "string" ? s.connection.spec_url : "";
  const services = Array.isArray(s.connection.services) ? (s.connection.services as string[]) : [];

  const disable = (t: GwTool) =>
    modal.confirm({
      title: `停用工具 ${t.name}`,
      okText: "停用",
      okButtonProps: { danger: true },
      content: "停用后对所有 Key 都不可用（tools/list 不再返回，调用被拒绝），和授权无关；授权保留，重新启用后恢复。",
      onOk: () =>
        tools.setEnabled.mutateAsync({ tool: t.name, on: false }).then(
          () => message.success(`已停用 ${s.name} · ${t.name}`),
          (e: Error) => message.error(e.message),
        ),
    });

  const head = {
    mcp: `工具定义从上游拉取 · 上次 ${relativeTime(s.fetched_at)} · 上游新增的工具拉到就能用`,
    rest: `${specUrl ? `从 OpenAPI 导入：${specUrl}` : "手工定义"} · 工具定义由网关维护，保存后生效`,
    grpc: `${s.connection.discovery === "descriptor" ? "描述文件" : "服务端反射"}发现${services.length ? `：${services.join("、")}` : ""} · 描述、副作用可以改`,
  }[s.kind];

  return (
    <>
      {s.fetch_error && (
        <Alert type="warning" showIcon style={{ marginBottom: 12 }} message="最近一次拉取失败，继续用旧定义" description={s.fetch_error} />
      )}
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
        <span className={styles.muted} style={{ fontSize: 12, flex: 1 }}>
          {head}
        </span>
        {manage && s.kind === "mcp" && (
          <Button
            size="small"
            icon={<Icon name="retry" size={13} />}
            loading={actions.refresh.isPending}
            disabled={!s.enabled}
            onClick={() =>
              actions.refresh.mutate(undefined, {
                onSuccess: () => message.success("已从上游重新拉取工具定义"),
                onError: (e) => message.error(e.message),
              })
            }
          >
            立即刷新
          </Button>
        )}
        {manage && s.kind === "rest" && specUrl && (
          <Button size="small" onClick={() => setRediscovering(true)}>
            重新导入
          </Button>
        )}
        {manage && s.kind === "grpc" && (
          <Button size="small" onClick={() => setRediscovering(true)}>
            重新发现
          </Button>
        )}
        {manage && s.kind === "rest" && (
          <Button size="small" type="primary" icon={<Icon name="plus" size={13} />} onClick={() => setViewing("new")}>
            添加接口
          </Button>
        )}
      </div>
      <Table<GwTool>
        size="small"
        rowKey="name"
        dataSource={s.tools}
        pagination={false}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有工具" /> }}
        columns={[
          {
            title: "工具",
            render: (_, t) => (
              <div>
                <span className={styles.toolName}>{t.name}</span>
                <div className={styles.desc2}>{t.description}</div>
              </div>
            ),
          },
          { title: "副作用", width: 100, render: (_, t) => <EffectTag effect={t.effect} /> },
          { title: "映射到", width: 220, render: (_, t) => mappingCell(s, t) },
          { title: "状态", width: 100, render: (_, t) => <ToolStatus t={t} /> },
          { title: "我的 Key", width: 100, render: (_, t) => myKeyCell(s, t) },
          {
            title: "",
            width: 170,
            render: (_, t) => (
              <Space size={0}>
                <Button size="small" type="link" onClick={() => setViewing(t)}>
                  {manage && s.kind !== "mcp" ? "编辑" : "查看"}
                </Button>
                <Button size="small" type="link" disabled={!t.callable || !s.enabled} onClick={() => setTrying(t)}>
                  试调
                </Button>
                {manage &&
                  (t.status === "active" ? (
                    <Button size="small" type="link" danger onClick={() => disable(t)}>
                      停用
                    </Button>
                  ) : (
                    <Button
                      size="small"
                      type="link"
                      onClick={() =>
                        tools.setEnabled.mutate(
                          { tool: t.name, on: true },
                          { onSuccess: () => message.success(`已启用 ${t.name}`), onError: (e) => message.error(e.message) },
                        )
                      }
                    >
                      启用
                    </Button>
                  ))}
              </Space>
            ),
          },
        ]}
      />
      {s.removed_tools.length > 0 && (
        <p className={styles.muted} style={{ marginTop: 8 }}>
          上游已删除：<span className={styles.del}>{s.removed_tools.join("、")}</span>（引用它们的智能体运行时会提示缺工具）
        </p>
      )}
      <p className={styles.muted} style={{ fontSize: 12, marginTop: 8 }}>
        tools/list 由网关按已存的定义生成，并按 Key 的权限过滤{s.kind === "mcp" ? "，不转发给上游" : ""}。停用的工具对所有 Key
        都不可用，和授权无关。
      </p>
      {viewing && <ToolModal service={s} tool={viewing === "new" ? null : viewing} onClose={() => setViewing(undefined)} />}
      {trying && <TryModal service={s} tool={trying} me={me} onClose={() => setTrying(undefined)} />}
      {rediscovering && <RediscoverModal service={s} onClose={() => setRediscovering(false)} />}
    </>
  );
}

// ─────────────────────────────── 工具定义：查看、编辑

interface SchemaProp {
  type?: string;
  description?: string;
}

function schemaParams(schema: Record<string, unknown>): { name: string; type: string; required: boolean; description: string }[] {
  const props = (schema.properties ?? {}) as Record<string, SchemaProp>;
  const required = new Set(Array.isArray(schema.required) ? (schema.required as string[]) : []);
  return Object.entries(props).map(([name, p]) => ({
    name,
    type: String(p?.type ?? "any"),
    required: required.has(name),
    description: String(p?.description ?? ""),
  }));
}

function ParamTable({ schema }: { schema: Record<string, unknown> }) {
  return (
    <Table
      size="small"
      rowKey="name"
      pagination={false}
      dataSource={schemaParams(schema)}
      locale={{ emptyText: "没有参数" }}
      columns={[
        { title: "参数", dataIndex: "name", render: (v: string) => <code>{v}</code> },
        { title: "类型", dataIndex: "type", width: 100, render: (v: string) => <code>{v}</code> },
        { title: "必填", dataIndex: "required", width: 60, render: (v: boolean) => (v ? "是" : "") },
        { title: "说明", dataIndex: "description" },
      ]}
    />
  );
}

const PARAM_IN: GwRestParam["in"][] = ["path", "query", "header", "body"];
const PARAM_TYPES = ["string", "integer", "number", "boolean", "array", "object"];
const METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"];

function restMappingOf(t: GwTool | null): GwRestMapping {
  const m = (t?.mapping ?? {}) as Partial<GwRestMapping>;
  return {
    method: m.method ?? "GET",
    path: m.path ?? "/",
    params: (m.params ?? []).map((p) => ({ ...p })),
    pick: m.pick ?? "",
    limit_kb: m.limit_kb ?? 32,
  };
}

/** 和网关生成 tools/list 的规则一致：固定值的参数不出现在工具参数里 */
function previewSchema(params: GwRestParam[]): Record<string, unknown> {
  const properties: Record<string, unknown> = {};
  const required: string[] = [];
  for (const p of params) {
    if (p.fixed) continue;
    properties[p.name] = {
      type: p.type,
      ...(p.type === "array" ? { items: { type: "string" } } : {}),
      ...(p.description ? { description: p.description } : {}),
    };
    if (p.required || p.in === "path") required.push(p.name);
  }
  return { type: "object", properties, ...(required.length ? { required } : {}) };
}

const annotationsOf = (effect: GwEffect) =>
  effect === "read" ? { readOnlyHint: true } : { readOnlyHint: false, destructiveHint: effect === "destructive" };

function ToolModal({ service: s, tool: t, onClose }: { service: GwServiceDetail; tool: GwTool | null; onClose: () => void }) {
  const { modal, message } = AntdApp.useApp();
  const actions = useGwToolActions(s.name);
  const isNew = t === null;
  const editable = s.can_manage && s.kind !== "mcp";
  const rest = s.kind === "rest";
  const [name, setName] = useState(t?.name ?? "");
  const [description, setDescription] = useState(t?.description ?? "");
  const [effect, setEffect] = useState<GwEffect>(t?.effect ?? "read");
  const [mapping, setMapping] = useState<GwRestMapping>(restMappingOf(t));
  const setParam = (i: number, patch: Partial<GwRestParam>) =>
    setMapping({ ...mapping, params: mapping.params.map((p, j) => (j === i ? { ...p, ...patch } : p)) });

  const preview = {
    name: t?.name ?? (name || "<工具名>"),
    description,
    inputSchema: rest && editable ? previewSchema(mapping.params) : (t?.input_schema ?? {}),
    annotations: annotationsOf(effect),
  };

  if (!editable && t) {
    const m = t.mapping ?? {};
    const base = String(s.connection.base_url ?? "");
    return (
      <Modal open width={820} title={`${s.name} · ${t.name}`} onCancel={onClose} footer={<Button onClick={onClose}>关闭</Button>}>
        <div className={styles.kv}>
          <span>描述</span>
          <span>{t.description}</span>
        </div>
        <div className={styles.kv}>
          <span>副作用</span>
          <span>
            <EffectTag effect={t.effect} />
            {s.kind === "mcp" && <span className={styles.muted}>取自上游注解；上游没标注只读的按「有副作用」处理</span>}
          </span>
        </div>
        <div className={styles.kv}>
          <span>状态</span>
          <span>
            <ToolStatus t={t} />
          </span>
        </div>
        <div className={styles.kv}>
          <span>映射到</span>
          <span>
            {s.kind === "rest" ? (
              <code>{`${String(m.method ?? "")} ${base}${String(m.path ?? "")}`}</code>
            ) : s.kind === "grpc" ? (
              <code>{`${String(m.method ?? "")}（${STREAM_LABEL[String(m.stream)] ?? ""}）`}</code>
            ) : (
              <>
                上游 <code>{String(s.connection.url ?? "")}</code> 的同名工具
              </>
            )}
          </span>
        </div>
        {t.issues.length > 0 && <Alert type="error" showIcon style={{ margin: "12px 0" }} message={t.issues.join("；")} />}
        <div className={styles.secTitle}>参数</div>
        <ParamTable schema={t.input_schema} />
        <details style={{ marginTop: 12 }}>
          <summary className={styles.muted}>MCP 工具定义（tools/list 返回给调用方的）</summary>
          <pre className={styles.code}>{json({ ...preview, annotations: t.annotations ?? preview.annotations })}</pre>
        </details>
      </Modal>
    );
  }

  const save = () => {
    if (!description.trim()) {
      message.error("请填写描述");
      return;
    }
    const toolName = t?.name ?? name;
    const done = (verb: string) => () => {
      message.success(`${verb} ${toolName}`);
      onClose();
    };
    const fail = (e: Error) => message.error(e.message);
    if (t === null) {
      actions.add.mutate({ name, description: description.trim(), effect, mapping }, { onSuccess: done("已添加"), onError: fail });
    } else {
      actions.update.mutate(
        { tool: t.name, description: description.trim(), effect, ...(rest ? { mapping } : {}) },
        { onSuccess: done("已保存"), onError: fail },
      );
    }
  };

  const del = () =>
    t &&
    modal.confirm({
      title: `删除工具 ${t.name}`,
      okText: "删除",
      okButtonProps: { danger: true },
      content: "删除后调用方不能再用；引用它的智能体在编辑器里会提示工具已不存在。授给 Key 的工具级授权一并删除。",
      onOk: () =>
        actions.remove.mutateAsync(t.name).then(
          () => {
            message.success(`已删除 ${t.name}`);
            onClose();
          },
          (e: Error) => message.error(e.message),
        ),
    });

  return (
    <Modal
      open
      width={980}
      title={t === null ? `添加接口 · ${s.display_name}` : `编辑工具 ${t.name}`}
      onCancel={onClose}
      footer={
        <div style={{ display: "flex" }}>
          {!isNew && rest && (
            <Button danger onClick={del}>
              删除工具
            </Button>
          )}
          <span style={{ flex: 1 }} />
          <Button onClick={onClose}>取消</Button>
          <Button type="primary" loading={actions.add.isPending || actions.update.isPending} onClick={save}>
            保存
          </Button>
        </div>
      }
    >
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <FormField
          label="工具名"
          hint={isNew ? "保存后不能改：它是智能体配置里的引用、用户中心里工具级授权的数据标识" : "不能修改"}
        >
          <Input value={name} disabled={!isNew} onChange={(e) => setName(e.target.value.trim())} placeholder="小写字母、数字、下划线" />
        </FormField>
        <FormField label="副作用" hint="写进 MCP 工具注解；Atlas 审批按它判断要不要确认">
          <Select
            value={effect}
            onChange={setEffect}
            options={Object.entries(GW_EFFECT).map(([value, e]) => ({ value, label: e.label }))}
          />
        </FormField>
      </div>
      <FormField label="描述">
        <Input.TextArea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="告诉模型这个工具做什么、什么时候用" />
      </FormField>
      {rest ? (
        <>
          <div className={styles.secTitle}>请求映射</div>
          <div style={{ display: "grid", gridTemplateColumns: "140px 1fr", gap: 12 }}>
            <FormField label="Method">
              <Select value={mapping.method} onChange={(method) => setMapping({ ...mapping, method })} options={METHODS.map((m) => ({ value: m, label: m }))} />
            </FormField>
            <FormField
              label="路径"
              hint={
                <>
                  相对 Base URL <code>{String(s.connection.base_url ?? "")}</code>；<code>{"{id}"}</code> 这样的占位符取位置为 path 的参数
                </>
              }
            >
              <Input value={mapping.path} onChange={(e) => setMapping({ ...mapping, path: e.target.value.trim() })} style={{ fontFamily: "var(--mono)" }} />
            </FormField>
          </div>
          <Table<GwRestParam & { i: number }>
            size="small"
            rowKey="i"
            pagination={false}
            dataSource={mapping.params.map((p, i) => ({ ...p, i }))}
            locale={{ emptyText: "没有参数" }}
            columns={[
              { title: "参数", width: 140, render: (_, p) => <Input size="small" value={p.name} onChange={(e) => setParam(p.i, { name: e.target.value.trim() })} /> },
              {
                title: "位置",
                width: 96,
                render: (_, p) => (
                  <Select size="small" style={{ width: "100%" }} value={p.in} onChange={(v) => setParam(p.i, { in: v })} options={PARAM_IN.map((x) => ({ value: x, label: x }))} />
                ),
              },
              {
                title: "类型",
                width: 104,
                render: (_, p) => (
                  <Select size="small" style={{ width: "100%" }} value={p.type} onChange={(v) => setParam(p.i, { type: v })} options={PARAM_TYPES.map((x) => ({ value: x, label: x }))} />
                ),
              },
              {
                title: "必填",
                width: 50,
                render: (_, p) => (
                  <input type="checkbox" checked={p.required || p.in === "path"} disabled={p.in === "path"} onChange={(e) => setParam(p.i, { required: e.target.checked })} />
                ),
              },
              {
                title: "上游字段",
                width: 110,
                render: (_, p) => (
                  <Tooltip title="发给上游时用的名字，缺省同参数名；$ = 整个请求体">
                    <Input size="small" value={p.field === p.name ? "" : p.field} placeholder="同参数名" onChange={(e) => setParam(p.i, { field: e.target.value.trim() || p.name })} />
                  </Tooltip>
                ),
              },
              { title: "固定值", width: 110, render: (_, p) => <Input size="small" value={p.fixed} placeholder="—" onChange={(e) => setParam(p.i, { fixed: e.target.value })} /> },
              { title: "说明（给模型看）", render: (_, p) => <Input size="small" value={p.description} onChange={(e) => setParam(p.i, { description: e.target.value })} /> },
              {
                title: "",
                width: 50,
                render: (_, p) => (
                  <Button size="small" type="link" danger onClick={() => setMapping({ ...mapping, params: mapping.params.filter((_x, j) => j !== p.i) })}>
                    删除
                  </Button>
                ),
              },
            ]}
          />
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 8 }}>
            <Button
              size="small"
              onClick={() =>
                setMapping({
                  ...mapping,
                  params: [...mapping.params, { name: "", in: "query", type: "string", required: false, fixed: "", description: "", field: "" }],
                })
              }
            >
              ＋ 添加参数
            </Button>
            <span className={styles.muted} style={{ fontSize: 12 }}>
              填了固定值的参数每次都按固定值发给上游，不出现在工具参数里
            </span>
          </div>
          <div className={styles.secTitle}>结果</div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <FormField label="只取响应里的字段（可选）" hint="JSONPath，如 $.items；不填返回整个响应体">
              <Input value={mapping.pick} onChange={(e) => setMapping({ ...mapping, pick: e.target.value.trim() })} placeholder="$.items" style={{ fontFamily: "var(--mono)" }} />
            </FormField>
            <FormField label="结果上限（KB）" hint="超过截断，并在结果里注明">
              <InputNumber min={1} max={1024} value={mapping.limit_kb} onChange={(v) => setMapping({ ...mapping, limit_kb: v ?? 32 })} />
            </FormField>
          </div>
          <div className={styles.muted} style={{ fontSize: 12 }}>
            非 2xx 响应作为工具错误返回（isError = true，带状态码和响应摘要）。
          </div>
        </>
      ) : (
        t && (
          <>
            <div className={styles.secTitle}>方法</div>
            <div className={styles.kv}>
              <span>gRPC 方法</span>
              <code>{String(t.mapping?.method ?? "")}</code>
            </div>
            <div className={styles.kv}>
              <span>流式</span>
              <span>
                {STREAM_LABEL[String(t.mapping?.stream)] ?? ""}
                {t.mapping?.stream === "server" ? " · 流里的消息合并成数组返回" : ""}
              </span>
            </div>
            <div className={styles.kv}>
              <span>请求消息</span>
              <code>{String(t.mapping?.request ?? "")}</code>
            </div>
            <div style={{ marginTop: 12 }}>
              <ParamTable schema={t.input_schema} />
            </div>
            <div className={styles.muted} style={{ fontSize: 12, marginTop: 8 }}>
              参数来自 proto 定义（按 proto3 JSON 映射）；要改参数，改 proto 后「重新发现」。
            </div>
          </>
        )
      )}
      <details style={{ marginTop: 12 }}>
        <summary className={styles.muted}>MCP 工具定义预览（tools/list 返回给调用方的）</summary>
        <pre className={styles.code}>{json(preview)}</pre>
      </details>
    </Modal>
  );
}

// ─────────────────────────────── 重新导入 / 重新发现

function RediscoverModal({ service: s, onClose }: { service: GwServiceDetail; onClose: () => void }) {
  const { message } = AntdApp.useApp();
  const actions = useGwServiceActions(s.name);
  const needsFile = s.kind === "grpc" && s.connection.discovery === "descriptor";
  const [descriptor, setDescriptor] = useState<string | null>(null);
  const [found, setFound] = useState<GwDiscovered | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const run = () =>
    actions.rediscover.mutate(descriptor, {
      onSuccess: (d) => {
        setFound(d);
        setPicked(defaultPicked(d));
      },
    });
  const title = s.kind === "rest" ? "重新导入 OpenAPI" : "重新发现方法";

  return (
    <Modal
      open
      width={880}
      title={`${title} · ${s.display_name}`}
      okText="导入选中的"
      okButtonProps={{ disabled: !picked.length, loading: actions.importTools.isPending }}
      onCancel={onClose}
      onOk={() =>
        actions.importTools.mutate(
          { selected: picked, descriptor },
          {
            onSuccess: (added) => {
              message.success(`已导入 ${added.length} 个工具`);
              onClose();
            },
            onError: (e) => message.error(e.message),
          },
        )
      }
    >
      <p className={styles.muted}>
        按保存的连接重新{s.kind === "rest" ? "读取接口定义" : "列出方法"}，挑出要新增的工具。已有的工具不会被改动：要改映射请编辑工具，不再需要的请停用或删除。
      </p>
      {needsFile && <DescriptorUpload value={descriptor} onChange={setDescriptor} />}
      <Button size="small" loading={actions.rediscover.isPending} disabled={needsFile && !descriptor} onClick={run}>
        {s.kind === "rest" ? "解析 OpenAPI" : "发现方法"}
      </Button>
      <DiscoverError error={actions.rediscover.error} />
      {found && (
        <div style={{ marginTop: 12 }}>
          <DiscoveredTable found={found} picked={picked} onPick={setPicked} />
        </div>
      )}
    </Modal>
  );
}

// ─────────────────────────────── 试调

function sampleArgs(schema: Record<string, unknown>): Record<string, unknown> {
  const ex: Record<string, unknown> = { string: "示例", integer: 1, number: 1.5, boolean: true, array: [], object: {} };
  return Object.fromEntries(schemaParams(schema).filter((p) => p.required).map((p) => [p.name, ex[p.type] ?? null]));
}

function TryModal({ service: s, tool: t, me, onClose }: { service: GwServiceDetail; tool: GwTool; me: GwMe; onClose: () => void }) {
  const actions = useGwToolActions(s.name);
  const myQ = useGwMyKey();
  const usersQ = useGwKeys("user", me.can_manage);
  const clientsQ = useGwKeys("client", me.can_manage);
  // 管理员可以选任意一把正常的 Key；其他人只能用自己的用户 Key
  const options = useMemo(() => {
    const label = (k: GwKey) => `${k.name || k.client_code}（${k.masked}）`;
    const active = (ks: GwKey[] | null | undefined) => (ks ?? []).filter((k) => k.status === "active");
    if (!me.can_manage)
      return [{ label: "我的用户 Key", options: active(myQ.data ? [myQ.data] : []).map((k) => ({ value: k.id, label: label(k) })) }];
    return [
      { label: "用户 Key", options: active(usersQ.data).map((k) => ({ value: k.id, label: label(k) })) },
      { label: "智能体 Key", options: active(clientsQ.data).map((k) => ({ value: k.id, label: label(k) })) },
    ];
  }, [me.can_manage, myQ.data, usersQ.data, clientsQ.data]);
  const [keyId, setKeyId] = useState<string>();
  const [args, setArgs] = useState(json(sampleArgs(t.input_schema)));
  const [out, setOut] = useState<GwTryOut | { parse: string }>();
  const fallback = me.can_manage ? undefined : myQ.data?.status === "active" ? myQ.data.id : undefined;
  const chosen = keyId ?? fallback;

  const run = () => {
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(args || "{}") as Record<string, unknown>;
    } catch (e) {
      setOut({ parse: (e as Error).message });
      return;
    }
    if (!chosen) return;
    actions.call.mutate({ tool: t.name, key_id: chosen, arguments: parsed }, { onSuccess: setOut, onError: (e) => setOut({ parse: e.message }) });
  };

  return (
    <Modal
      open
      width={820}
      title={`试调 ${s.name} · ${t.name}`}
      okText="调用"
      okButtonProps={{ disabled: !chosen, loading: actions.call.isPending }}
      onOk={run}
      onCancel={onClose}
      cancelText="关闭"
    >
      <FormField
        label="用哪把 Key 调用"
        hint={
          <>
            和真实调用一样：认 Key → 以 Key 的主体作 X-Auth-Uid 调用户中心判权 → 调上游。
            {t.effect !== "read" && <b> 这个工具有副作用，会真的执行。</b>}
          </>
        }
      >
        {!me.can_manage && !fallback ? (
          <Alert type="warning" showIcon message="你还没有可用的用户 Key：先到「API Key」申请" />
        ) : (
          <Select style={{ width: "100%" }} value={chosen} onChange={setKeyId} options={options} placeholder="选择 Key" showSearch optionFilterProp="label" />
        )}
      </FormField>
      <FormField label="参数（JSON）">
        <Input.TextArea rows={6} value={args} onChange={(e) => setArgs(e.target.value)} style={{ fontFamily: "var(--mono)" }} />
      </FormField>
      {out && "parse" in out && <Alert type="error" showIcon message={out.parse} />}
      {out && !("parse" in out) && (
        <>
          <div className={styles.secTitle}>
            {out.error ? (out.error.data?.forwarded ? "上游返回了错误" : "网关直接返回（没有转发给上游）") : "返回给调用方的 MCP 结果"}
          </div>
          <pre className={styles.code}>{json(out.error ?? out.result)}</pre>
          {out.trace_id && (
            <div className={styles.muted} style={{ fontSize: 12 }}>
              trace_id <Typography.Text copyable code>{out.trace_id}</Typography.Text>（在追踪系统里按它查这次调用的 Span）
            </div>
          )}
        </>
      )}
    </Modal>
  );
}

// ═══════════════════════════════════════════════ 连接与凭据

function ConnTab({ service: s }: { service: GwServiceDetail }) {
  const { message } = AntdApp.useApp();
  const actions = useGwServiceActions(s.name);
  const [editing, setEditing] = useState<"conn" | "cred">();
  const ids = s.kind === "grpc" ? "metadata x-auth-uid（Key 的主体）、x-uid（使用者）" : "请求头 X-Auth-Uid（Key 的主体）、X-Uid（使用者）";
  const traceparent = { mcp: "请求头 traceparent，并写进 params._meta", rest: "请求头 traceparent", grpc: "metadata traceparent" }[s.kind];

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 24 }}>
      <div>
        <div className={styles.secTitle}>
          连接
          <span style={{ flex: 1 }} />
          {s.can_manage && (
            <>
              <Button
                size="small"
                loading={actions.test.isPending}
                onClick={() =>
                  actions.test.mutate(undefined, {
                    onSuccess: (r) => (r.ok ? message.success(r.detail) : message.error(r.detail)),
                    onError: (e) => message.error(e.message),
                  })
                }
              >
                测试连接
              </Button>
              <Button size="small" onClick={() => setEditing("conn")}>
                编辑
              </Button>
            </>
          )}
        </div>
        {connectionRows(s.kind, s.connection).map(([k, v]) => (
          <div key={k} className={styles.kv}>
            <span>{k}</span>
            <span style={{ textAlign: "right", wordBreak: "break-all" }}>{v}</span>
          </div>
        ))}
      </div>
      <div>
        <div className={styles.secTitle}>
          上游凭据与透传
          <span style={{ flex: 1 }} />
          {s.can_manage && (
            <Button size="small" onClick={() => setEditing("cred")}>
              替换凭据
            </Button>
          )}
        </div>
        <div className={styles.kv}>
          <span>上游凭据</span>
          <span>{credentialLabel(s.credential)}</span>
        </div>
        <div className={styles.kv}>
          <span>调用者身份</span>
          <span>{s.pass_identity ? `${ids}，以及 X-Run-Id、X-Thread-Id` : "不传（上游不区分调用者）"}</span>
        </div>
        <div className={styles.kv}>
          <span>追踪上下文</span>
          <span>{traceparent}</span>
        </div>
        <div className={styles.kv}>
          <span>调用方的 Key</span>
          <span>不转发：网关认完 Key 就去掉 Authorization</span>
        </div>
      </div>
      {editing === "conn" && <ConnectionModal service={s} onClose={() => setEditing(undefined)} />}
      {editing === "cred" && <CredentialModal service={s} onClose={() => setEditing(undefined)} />}
    </div>
  );
}

function ConnectionModal({ service: s, onClose }: { service: GwServiceDetail; onClose: () => void }) {
  const { message } = AntdApp.useApp();
  const actions = useGwServiceActions(s.name);
  const [connection, setConnection] = useState<Connection>(connectionOf(s.kind, s.connection));
  const [passIdentity, setPassIdentity] = useState(s.pass_identity);
  return (
    <Modal
      open
      width={720}
      title={`编辑连接 · ${s.display_name}`}
      okText="保存"
      okButtonProps={{ loading: actions.connection.isPending }}
      onCancel={onClose}
      onOk={() =>
        actions.connection.mutate(
          { connection: connectionBody(s.kind, connection), pass_identity: passIdentity },
          {
            onSuccess: () => {
              message.success(s.kind === "mcp" ? "已保存连接，并重新拉取了工具定义" : "已保存连接");
              onClose();
            },
            onError: (e) => message.error(e.message),
          },
        )
      }
    >
      <ConnectionFields kind={s.kind} value={connection} onChange={setConnection} />
      <IdentityCheckbox kind={s.kind} value={passIdentity} onChange={setPassIdentity} />
    </Modal>
  );
}

function CredentialModal({ service: s, onClose }: { service: GwServiceDetail; onClose: () => void }) {
  const { message } = AntdApp.useApp();
  const actions = useGwServiceActions(s.name);
  const [credential, setCredential] = useState<GwCredentialIn>(credentialOf(s.credential));
  return (
    <Modal
      open
      width={640}
      title={`替换上游凭据 · ${s.display_name}`}
      okText="保存"
      okButtonProps={{ loading: actions.credential.isPending }}
      onCancel={onClose}
      onOk={() =>
        actions.credential.mutate(credentialBody(credential), {
          onSuccess: () => {
            message.success("已替换上游凭据：从下一次调用开始用");
            onClose();
          },
          onError: (e) => message.error(e.message),
        })
      }
    >
      <CredentialFields kind={s.kind} value={credential} onChange={setCredential} saved={s.credential} />
    </Modal>
  );
}

function EditServiceModal({ service: s, onClose }: { service: GwServiceDetail; onClose: () => void }) {
  const { message } = AntdApp.useApp();
  const actions = useGwServiceActions(s.name);
  const [displayName, setDisplayName] = useState(s.display_name);
  const [description, setDescription] = useState(s.description);
  const [visibility, setVisibility] = useState<GwVisibility>(s.visibility);
  return (
    <Modal
      open
      title={`编辑「${s.display_name}」`}
      okText="保存"
      okButtonProps={{ loading: actions.patch.isPending }}
      onCancel={onClose}
      onOk={() =>
        actions.patch.mutate(
          { display_name: displayName.trim(), description: description.trim(), visibility },
          {
            onSuccess: () => {
              message.success("已保存");
              onClose();
            },
            onError: (e) => message.error(e.message),
          },
        )
      }
    >
      <FormField label="服务名" hint="登记后不能修改">
        <Input value={s.name} disabled />
      </FormField>
      <FormField label="显示名称">
        <Input value={displayName} onChange={(e) => setDisplayName(e.target.value)} maxLength={100} />
      </FormField>
      <FormField label="描述">
        <Input.TextArea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
      </FormField>
      <FormField
        label="可见性"
        hint={
          visibility === s.visibility ? undefined : visibility === "public" ? (
            "改成公开后，任何有效的 Key 都能用全部工具；现有授权保留但不起作用，改回私有时恢复。"
          ) : (
            <span style={{ color: "var(--danger)" }}>改成私有后，没有授权的 Key 立即不能再用（各实例的判权缓存最长 30 秒）。</span>
          )
        }
      >
        <Radio.Group value={visibility} onChange={(e) => setVisibility(e.target.value)}>
          <Radio value="private">私有：按授给 Key 的权限调用</Radio>
          <Radio value="public">公开：任何 Key 都能用全部工具</Radio>
        </Radio.Group>
      </FormField>
    </Modal>
  );
}

// ═══════════════════════════════════════════════ 被引用（Atlas）

function RefsTab({ name }: { name: string }) {
  const refsQ = useMcpServer(name);
  const refs = (refsQ.data?.referenced_by ?? []) as Array<Record<string, unknown>>;
  if (refsQ.isPending) return <Skeleton active paragraph={{ rows: 3 }} />;
  if (refsQ.error) return <Alert type="warning" showIcon message={`Atlas 暂时取不到引用：${refsQ.error.message}`} />;
  if (!refs.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有智能体引用它" />;
  return (
    <>
      {refs.map((r) => (
        <div key={`${String(r.agent_id)}-${String(r.subagent ?? "")}`} className={styles.kv}>
          <span>
            <Link href={`/agents/${String(r.agent_id)}`}>{String(r.agent_name)}</Link> v{String(r.agent_version)}
            {r.subagent ? ` → ${String(r.subagent)}` : ""}
          </span>
          <span>{(r.tools as string[]).join(", ")}</span>
        </div>
      ))}
    </>
  );
}
