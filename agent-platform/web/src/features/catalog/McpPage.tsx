"use client";

/**
 * MCP 服务列表：按「和我的关系」筛（我能用的 / 我管理的 / 全部），卡片点进详情页。
 *
 * ★ 公开服务谁都能用；私有服务要有人给你的 Key 授权（可以细到工具）。管理员能接入新服务。
 */
import { Alert, App as AntdApp, Button, Empty, Input, Modal, Radio, Segmented, Skeleton, Tag } from "antd";
import { useRouter } from "next/navigation";
import { type ReactNode, useMemo, useState } from "react";

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
  connectionBody,
  credentialBody,
  defaultPicked,
  emptyConnection,
} from "./McpForms";

/** MCP 各页共用：先拿到「我」在网关上的身份，拿不到就整页报错 */
export function GwGate({ children }: { children: (me: GwMe) => ReactNode }) {
  const meQ = useGwMe();
  if (meQ.error) {
    return (
      <div className={pageStyles.body}>
        <Alert type="error" showIcon message="暂时连不上 MCP 服务" description={meQ.error.message} />
      </div>
    );
  }
  if (!meQ.data) {
    return (
      <div className={pageStyles.body}>
        <Skeleton active paragraph={{ rows: 6 }} />
      </div>
    );
  }
  return <>{children(meQ.data)}</>;
}

export function McpPage() {
  return <GwGate>{(me) => <ServicesList me={me} />}</GwGate>;
}

/** 当前用户的权限：所有者；或他的 Key 能用到什么 */
export function MyAccess({ s }: { s: GwService }) {
  if (s.my.owner) return <Tag color="gold">我管理的</Tag>;
  switch (s.my.scope) {
    case "public":
      return <Tag color="success">可使用</Tag>;
    case "all":
      return <Tag color="success">可使用</Tag>;
    case "tools":
      return <Tag color="success">可使用 {s.my.tools.length} 个工具</Tag>;
    case "no_key":
      return <span className={styles.muted}>先申请 Key</span>;
    default:
      return <span className={styles.muted}>需授权</span>;
  }
}

const canUse = (s: GwService) => s.my.owner || ["public", "all", "tools"].includes(s.my.scope);

type Scope = "use" | "mine" | "all";

function ServicesList({ me }: { me: GwMe }) {
  const router = useRouter();
  const servicesQ = useGwServices();
  const [scope, setScope] = useState<Scope>("use");
  const [q, setQ] = useState("");
  const [registering, setRegistering] = useState(false);
  const services = servicesQ.data ?? [];
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return services.filter(
      (s) =>
        (scope === "all" || (scope === "mine" ? s.my.owner : canUse(s))) &&
        (!needle || `${s.name} ${s.display_name} ${s.description}`.toLowerCase().includes(needle)),
    );
  }, [services, q, scope]);
  const open = (name: string) => router.push(`/mcp/services/${encodeURIComponent(name)}`);

  return (
    <div className={pageStyles.page}>
      <header className={pageStyles.head}>
        <div className={pageStyles.titleRow}>
          <h1 className={pageStyles.title}>服务</h1>
          {me.can_manage && (
            <Button type="primary" className={pageStyles.action} icon={<Icon name="plus" size={14} />} onClick={() => setRegistering(true)}>
              接入服务
            </Button>
          )}
        </div>
        <div className={pageStyles.toolbar}>
          <Segmented
            value={scope}
            onChange={(v) => setScope(v as Scope)}
            options={[
              { value: "use", label: "我能用的" },
              { value: "mine", label: "我管理的" },
              ...(me.can_manage ? [{ value: "all", label: "全部" }] : []),
            ]}
          />
          <Input
            allowClear
            style={{ width: 260 }}
            prefix={<Icon name="search" size={14} style={{ color: "var(--fg-4)" }} />}
            placeholder="搜索服务"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
      </header>
      <div className={pageStyles.body}>
        {servicesQ.error && <Alert type="error" showIcon style={{ marginBottom: 12 }} message={servicesQ.error.message} />}
        {servicesQ.isPending ? (
          <Skeleton active paragraph={{ rows: 6 }} />
        ) : rows.length === 0 ? (
          <Empty
            style={{ marginTop: 60 }}
            description={q ? "没有找到，换个关键词试试" : scope === "mine" ? "你还没有管理的服务" : "还没有你能用的服务"}
          />
        ) : (
          <div className={pageStyles.grid}>
            {rows.map((s) => (
              <div
                key={s.name}
                className={pageStyles.card}
                role="button"
                tabIndex={0}
                onClick={() => open(s.name)}
                onKeyDown={(e) => e.key === "Enter" && open(s.name)}
              >
                <div className={pageStyles.cardHead}>
                  <span className={pageStyles.avatar} style={{ background: "#e0f2fe", color: "#0369a1" }}>
                    <Icon name="plug" size={18} />
                  </span>
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div className={pageStyles.cardName}>
                      {s.display_name}
                      {s.visibility === "public" && <Tag style={{ marginInlineEnd: 0 }}>公开</Tag>}
                      {!s.enabled && <Tag style={{ marginInlineEnd: 0 }}>已停用</Tag>}
                    </div>
                    <div className={pageStyles.cardSub} style={{ fontFamily: "var(--mono)" }}>
                      {s.name}
                    </div>
                  </div>
                </div>
                <div className={pageStyles.cardDesc}>{s.description || "（没有描述）"}</div>
                <div className={pageStyles.cardFoot}>
                  <span>{s.active_tool_count} 个工具</span>
                  {s.fetch_error && <Tag color="warning">工具同步失败</Tag>}
                  <span style={{ flex: 1 }} />
                  <MyAccess s={s} />
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
      {registering && (
        <RegisterModal
          onClose={() => setRegistering(false)}
          onCreated={(name) => {
            setRegistering(false);
            open(name);
          }}
        />
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════ 登记

const DISCOVER_LABEL: Record<GwKind, { button: string; hint: string; first: string }> = {
  mcp: { button: "连接并读取工具", hint: "连上服务，读取它提供的工具", first: "请先连接并读取工具" },
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
      message.error("服务标识：小写字母开头，2–24 位小写字母、数字、连字符");
      return;
    }
    if (!displayName.trim()) {
      message.error("请填写名称");
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
          message.success(`已接入「${d.display_name}」，${d.tools.length} 个工具`);
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
      title="接入服务"
      okText="接入"
      okButtonProps={{ loading: create.isPending }}
      onCancel={onClose}
      onOk={submit}
    >
      <div className={styles.secTitle}>接入方式</div>
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
        <FormField label="服务标识" hint="接入后不能修改">
          <Input value={name} onChange={(e) => setName(e.target.value.trim().toLowerCase())} placeholder="小写字母、数字、连字符，如 crm" />
        </FormField>
        <FormField label="名称">
          <Input value={displayName} onChange={(e) => setDisplayName(e.target.value)} maxLength={100} />
        </FormField>
      </div>
      <FormField label="描述">
        <Input value={description} onChange={(e) => setDescription(e.target.value)} />
      </FormField>
      <FormField label="谁能用">
        <Radio.Group value={visibility} onChange={(e) => setVisibility(e.target.value)}>
          <Radio value="private">授权的人和智能体（可以细到工具）</Radio>
          <Radio value="public">所有人</Radio>
        </Radio.Group>
      </FormField>

      <div className={styles.secTitle}>连接</div>
      <ConnectionFields
        kind={kind}
        value={connection}
        onChange={setConnection}
        descriptor={{ value: descriptor, onChange: setDescriptor }}
      />

      <div className={styles.secTitle}>认证</div>
      <CredentialFields kind={kind} value={credential} onChange={setCredential} />
      <IdentityCheckbox kind={kind} value={passIdentity} onChange={setPassIdentity} />

      <div className={styles.secTitle}>工具</div>
      {manual ? (
        <div className={styles.muted}>接入后在服务的「工具」里逐个添加接口。</div>
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
