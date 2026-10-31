"use client";

/**
 * MCP 管理（原型 agent-platform/doc/prototype/mcp-gateway.html）：后端是 MCP 网关。
 *
 *   服务      连接原生 MCP Server，或把 REST / gRPC 接口转换成 MCP 工具。公开服务任何 Key 都能调用；
 *             私有服务按授给 API Key 的数据权限调用，可以细到工具
 *   API Key   用户 Key（本人申请，给外部客户端用）；智能体 Key（Atlas 建智能体时申请）
 *
 * MCP 管理员看到全部、能登记和改服务；服务的所有者能给 Key 授权；其他人看到公开服务和自己的 Key 能用的。
 */
import { Alert, App as AntdApp, Button, Drawer, Empty, Input, Modal, Radio, Segmented, Select, Table, Tabs, Tag } from "antd";
import { useMemo, useState } from "react";

import {
  GW_KIND,
  type GwCredentialIn,
  type GwDiscovered,
  type GwKind,
  type GwMe,
  type GwService,
  type GwVisibility,
  useCreateGwService,
  useGwDiscover,
  useGwMe,
  useGwServices,
} from "@/api/mcp-gateway";
import { Icon } from "@/components/Icon";
import pageStyles from "@/features/agents/agents.module.css";
import styles from "./catalog.module.css";
import {
  type Connection,
  ConnectionFields,
  CredentialFields,
  DiscoverError,
  DiscoveredTable,
  FormField,
  IdentityCheckbox,
  KindTag,
  VisibilityTag,
  connectionBody,
  credentialBody,
  defaultPicked,
  emptyConnection,
} from "./McpForms";
import { McpKeys } from "./McpKeys";
import { ServiceDetail } from "./McpService";

export function McpPage() {
  const meQ = useGwMe();

  if (meQ.error) {
    return (
      <div className={pageStyles.body}>
        <Alert
          type="error"
          showIcon
          message="连不上 MCP 网关"
          description={`MCP 的管理端在 MCP 网关上（atlas-mcpgateway）：${meQ.error.message}`}
        />
      </div>
    );
  }

  return (
    <div className={pageStyles.page}>
      <header className={pageStyles.head}>
        <div className={pageStyles.titleRow}>
          <div>
            <h1 className={pageStyles.title}>MCP</h1>
            <p className={pageStyles.desc}>
              连接原生 MCP Server，或把 REST / gRPC 接口转换成 MCP 工具，统一经 MCP 网关调用。公开服务任何 API Key
              都能调用；私有服务按授给 Key 的权限调用，可以细到工具。模型看到的名字是 服务名__工具名。
            </p>
          </div>
        </div>
      </header>
      <div className={pageStyles.body} style={{ paddingTop: 0 }}>
        {meQ.data && (
          <Tabs
            items={[
              { key: "services", label: "服务", children: <ServicesTab me={meQ.data} /> },
              { key: "keys", label: "API Key", children: <McpKeys me={meQ.data} /> },
            ]}
          />
        )}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════ 服务列表

/** 当前用户的权限：所有者；或他的用户 Key 能用到什么 */
export function MyAccess({ s }: { s: GwService }) {
  if (s.my.owner) return <Tag color="gold">所有者</Tag>;
  switch (s.my.scope) {
    case "public":
      return <span className={styles.muted}>公开</span>;
    case "all":
      return <Tag color="blue">我的 Key · 全部工具</Tag>;
    case "tools":
      return <Tag color="blue">我的 Key · {s.my.tools.length} 个工具</Tag>;
    case "no_key":
      return <span className={styles.muted}>还没有 Key</span>;
    default:
      return <span className={styles.muted}>—</span>;
  }
}

function ServicesTab({ me }: { me: GwMe }) {
  const servicesQ = useGwServices();
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<GwKind>();
  const [vis, setVis] = useState<GwVisibility>();
  const [open, setOpen] = useState<string>();
  const [registering, setRegistering] = useState(false);
  const services = servicesQ.data ?? [];
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return services.filter(
      (s) =>
        (!kind || s.kind === kind) &&
        (!vis || s.visibility === vis) &&
        (!needle || `${s.name} ${s.display_name} ${s.description}`.toLowerCase().includes(needle)),
    );
  }, [services, q, kind, vis]);

  return (
    <>
      {servicesQ.error && <Alert type="error" showIcon style={{ marginBottom: 12 }} message={servicesQ.error.message} />}
      <div className={pageStyles.filterRow}>
        <Input allowClear placeholder="搜索服务名、名称、描述" style={{ width: 240 }} value={q} onChange={(e) => setQ(e.target.value)} />
        <Select
          allowClear
          placeholder="全部类型"
          style={{ width: 130 }}
          value={kind}
          onChange={setKind}
          options={Object.entries(GW_KIND).map(([value, k]) => ({ value, label: k.label }))}
        />
        <Select
          allowClear
          placeholder="公开和私有"
          style={{ width: 130 }}
          value={vis}
          onChange={setVis}
          options={[
            { value: "public", label: "公开" },
            { value: "private", label: "私有" },
          ]}
        />
        <span className={styles.muted} style={{ marginLeft: "auto" }}>
          {rows.length} 个服务
        </span>
        {me.can_manage && (
          <Button type="primary" icon={<Icon name="plus" size={14} />} onClick={() => setRegistering(true)}>
            登记服务
          </Button>
        )}
      </div>
      <Table<GwService>
        size="middle"
        rowKey="name"
        loading={servicesQ.isPending}
        dataSource={rows}
        pagination={false}
        onRow={(s) => ({ onClick: () => setOpen(s.name), style: { cursor: "pointer" } })}
        locale={{
          emptyText: (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={services.length ? "没有符合条件的服务" : me.can_manage ? "还没有 MCP 服务" : "还没有你能用的 MCP 服务"}
            />
          ),
        }}
        columns={[
          {
            title: "服务",
            render: (_, s) => (
              <div>
                <b>{s.display_name}</b>
                <div className={styles.desc2} style={{ fontFamily: "var(--mono)" }}>
                  {s.name}
                </div>
              </div>
            ),
          },
          { title: "类型", width: 120, render: (_, s) => <KindTag kind={s.kind} /> },
          { title: "可见性", width: 90, render: (_, s) => <VisibilityTag visibility={s.visibility} /> },
          {
            title: "工具",
            width: 120,
            render: (_, s) => (
              <span>
                {s.active_tool_count} / {s.tool_count} 可用
              </span>
            ),
          },
          {
            title: "状态",
            width: 160,
            render: (_, s) => (
              <span>
                {s.enabled ? <Tag color="success">启用</Tag> : <Tag>已停用</Tag>}
                {s.fetch_error && (
                  <Tag color="warning" title={s.fetch_error}>
                    拉取失败
                  </Tag>
                )}
              </span>
            ),
          },
          { title: "我的权限", width: 170, render: (_, s) => <MyAccess s={s} /> },
        ]}
      />
      <Drawer open={!!open} width={1000} onClose={() => setOpen(undefined)} destroyOnClose title={null}>
        {open && <ServiceDetail name={open} me={me} onClose={() => setOpen(undefined)} />}
      </Drawer>
      {registering && (
        <RegisterModal
          onClose={() => setRegistering(false)}
          onCreated={(name) => {
            setRegistering(false);
            setOpen(name);
          }}
        />
      )}
    </>
  );
}

// ═══════════════════════════════════════════════ 登记

const DISCOVER_LABEL: Record<GwKind, { button: string; hint: string; first: string }> = {
  mcp: { button: "连接并拉取工具", hint: "调上游 initialize + tools/list", first: "请先连接并拉取工具" },
  rest: { button: "解析 OpenAPI", hint: "读取接口定义，每个操作转成一个工具", first: "请先解析 OpenAPI" },
  grpc: { button: "发现方法", hint: "通过服务端反射或描述文件列出服务和方法", first: "请先发现方法" },
};

function RegisterModal({ onClose, onCreated }: { onClose: () => void; onCreated: (name: string) => void }) {
  const { message } = AntdApp.useApp();
  const create = useCreateGwService();
  const discover = useGwDiscover();
  const [kind, setKind] = useState<GwKind>("mcp");
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [visibility, setVisibility] = useState<GwVisibility>("private");
  const [connection, setConnectionRaw] = useState<Connection>(emptyConnection("mcp"));
  const [credential, setCredentialRaw] = useState<GwCredentialIn>({ type: "none" });
  const [descriptor, setDescriptorRaw] = useState<string | null>(null);
  const [passIdentity, setPassIdentity] = useState(true);
  const [found, setFound] = useState<GwDiscovered | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const manual = kind === "rest" && connection.source === "manual";

  // 连接、凭据、描述文件变了：之前的发现结果作废
  const stale = () => {
    setFound(null);
    setPicked([]);
    discover.reset();
  };
  const setConnection = (c: Connection) => {
    setConnectionRaw(c);
    stale();
  };
  const setCredential = (c: GwCredentialIn) => {
    setCredentialRaw(c);
    stale();
  };
  const setDescriptor = (d: string | null) => {
    setDescriptorRaw(d);
    stale();
  };
  const switchKind = (k: GwKind) => {
    setKind(k);
    setConnectionRaw(emptyConnection(k));
    setCredentialRaw({ type: "none" });
    setDescriptorRaw(null);
    stale();
  };

  const body = () => ({
    kind,
    connection: connectionBody(kind, connection),
    credential: credentialBody(credential),
    descriptor: kind === "grpc" ? descriptor : null,
  });

  const runDiscover = () =>
    discover.mutate(body(), {
      onSuccess: (d) => {
        setFound(d);
        setPicked(defaultPicked(d));
      },
    });

  const submit = () => {
    if (!/^[a-z][a-z0-9-]{1,23}$/.test(name)) {
      message.error("服务名：小写字母开头，2–24 位小写字母、数字、连字符");
      return;
    }
    if (!displayName.trim()) {
      message.error("请填写显示名称");
      return;
    }
    if (!manual && !found) {
      message.error(DISCOVER_LABEL[kind].first);
      return;
    }
    if (kind !== "mcp" && !manual && picked.length === 0) {
      message.error(`请至少选一个${kind === "rest" ? "接口" : "方法"}`);
      return;
    }
    create.mutate(
      {
        ...body(),
        name,
        display_name: displayName.trim(),
        description: description.trim(),
        visibility,
        pass_identity: passIdentity,
        selected: kind === "mcp" || manual ? [] : picked,
      },
      {
        onSuccess: (d) => {
          message.success(`已登记「${d.display_name}」，${d.tools.length} 个工具。你是它的所有者`);
          onCreated(d.name);
        },
        onError: (e) => message.error(e.message),
      },
    );
  };

  return (
    <Modal
      open
      width={880}
      title="登记 MCP 服务"
      okText="登记"
      okButtonProps={{ loading: create.isPending }}
      onCancel={onClose}
      onOk={submit}
    >
      <div className={styles.secTitle}>类型</div>
      <Segmented
        value={kind}
        onChange={(v) => switchKind(v as GwKind)}
        options={Object.entries(GW_KIND).map(([value, k]) => ({ value, label: k.label }))}
      />
      <div className={styles.muted} style={{ fontSize: 12, marginTop: 6 }}>
        {GW_KIND[kind].desc}
      </div>

      <div className={styles.secTitle}>基本信息</div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <FormField label="服务名" hint="工具名前缀、用户中心数据标识、服务地址的一段；登记后不能改，也不能复用">
          <Input value={name} onChange={(e) => setName(e.target.value.trim().toLowerCase())} placeholder="小写字母、数字、连字符，如 crm" />
        </FormField>
        <FormField label="显示名称">
          <Input value={displayName} onChange={(e) => setDisplayName(e.target.value)} maxLength={100} />
        </FormField>
      </div>
      <FormField label="描述">
        <Input value={description} onChange={(e) => setDescription(e.target.value)} />
      </FormField>
      <FormField label="可见性">
        <Radio.Group value={visibility} onChange={(e) => setVisibility(e.target.value)}>
          <Radio value="private">私有：按授给 Key 的权限调用（可以细到工具）</Radio>
          <Radio value="public">公开：任何 Key 都能用全部工具，不需要授权</Radio>
        </Radio.Group>
      </FormField>

      <div className={styles.secTitle}>连接</div>
      <ConnectionFields
        kind={kind}
        value={connection}
        onChange={setConnection}
        descriptor={{ value: descriptor, onChange: setDescriptor }}
      />

      <div className={styles.secTitle}>上游凭据与透传</div>
      <CredentialFields kind={kind} value={credential} onChange={setCredential} />
      <IdentityCheckbox kind={kind} value={passIdentity} onChange={setPassIdentity} />

      <div className={styles.secTitle}>工具</div>
      {manual ? (
        <div className={styles.muted}>手工添加：登记后在服务的「工具」里逐个添加接口。</div>
      ) : (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
            <Button size="small" loading={discover.isPending} onClick={runDiscover}>
              {DISCOVER_LABEL[kind].button}
            </Button>
            <span className={styles.muted} style={{ fontSize: 12 }}>
              {DISCOVER_LABEL[kind].hint}
            </span>
          </div>
          <DiscoverError error={discover.error} />
          {found && <DiscoveredTable found={found} picked={picked} onPick={setPicked} />}
        </>
      )}
    </Modal>
  );
}
