"use client";

/**
 * API Key（参考模型管理平台的 KeysTab）：权限挂在 Key 上。每把 Key 在用户中心有自己的主体，
 * 网关判权时以它作 X-Auth-Uid。
 *
 *   用户 Key     每人一把，只能为本人申请，给 Claude Code、Cursor 这类外部客户端用。原文只在申请、
 *                重置时显示一次。申请后马上能调公开服务；私有服务要所有者或管理员给它分配权限
 *   智能体 Key   一个智能体一把（客户端类型 Agent，编码 = 智能体 id）。Atlas 建智能体时申请、删智能体时
 *                删除；管理员可以给还没有 Key 的智能体补申请。原文不在页面上显示
 */
import { Alert, App as AntdApp, Button, Empty, Input, Modal, Segmented, Select, Space, Table, Tag, Typography } from "antd";
import { useMemo, useState } from "react";

import { useAgents } from "@/api/agents";
import {
  GW_CLIENT_TYPE,
  GW_KEY_STATUS,
  type GwKey,
  type GwKeySecret,
  type GwMe,
  type GwService,
  useGwKeyActions,
  useGwKeys,
  useGwMyKey,
  useGwMyKeyActions,
  useGwServices,
} from "@/api/mcp-gateway";
import { Icon } from "@/components/Icon";
import pageStyles from "@/features/agents/agents.module.css";
import { useMe } from "@/lib/session";
import styles from "./catalog.module.css";
import { AssignModal, KeyScopes } from "./McpGrants";
import { FormField, fmtTime } from "./McpForms";

export function McpKeys({ me }: { me: GwMe }) {
  const [tab, setTab] = useState<"user" | "client">("user");
  const clientsQ = useGwKeys("client", me.can_manage);
  return (
    <>
      {me.can_manage && (
        <Segmented
          style={{ marginBottom: 12 }}
          value={tab}
          onChange={(v) => setTab(v as "user" | "client")}
          options={[
            { value: "user", label: "用户 Key" },
            {
              value: "client",
              label: (
                <span>
                  智能体 Key {clientsQ.data && <span className={styles.muted}>{clientsQ.data.length}</span>}
                </span>
              ),
            },
          ]}
        />
      )}
      {tab === "client" && me.can_manage ? (
        <AgentKeys keys={clientsQ.data ?? []} loading={clientsQ.isPending} error={clientsQ.error} />
      ) : (
        <UserKeys me={me} />
      )}
    </>
  );
}

function KeyStatusTag({ k }: { k: GwKey }) {
  const s = GW_KEY_STATUS[k.status];
  return <Tag color={s.color}>{s.label}</Tag>;
}

function KeyMasked({ k }: { k: GwKey }) {
  return (
    <div>
      <code>{k.masked}</code>
      <div className={styles.desc2} style={{ fontFamily: "var(--mono)" }} title="用户中心里的主体：授权授给它">
        主体 {k.subject_uid.slice(0, 8)}…
      </div>
    </div>
  );
}

/** 外部客户端的配置示例：每个服务一个地址 */
function clientConfig(me: GwMe, names: string[], secret = "${MCP_API_KEY}"): string {
  const base = me.public_url ?? "<MCP 网关地址，请咨询管理员>";
  const servers = Object.fromEntries(
    (names.length ? names : ["<服务名>"]).map((n) => [
      n,
      { type: "http", url: `${base}/mcp/${n}`, headers: { Authorization: `Bearer ${secret}` } },
    ]),
  );
  return JSON.stringify({ mcpServers: servers }, null, 2);
}

/** 我的 Key 能用的服务：公开的（启用的）+ 授给它的 */
function usable(services: GwService[]): GwService[] {
  return services.filter((s) => s.enabled && ["public", "all", "tools"].includes(s.my.scope));
}

// ═══════════════════════════════════════════════ 用户 Key

function UserKeys({ me }: { me: GwMe }) {
  const { modal, message } = AntdApp.useApp();
  const atlasMe = useMe();
  const myQ = useGwMyKey();
  const othersQ = useGwKeys("user", me.can_manage);
  const servicesQ = useGwServices();
  const mine = useGwMyKeyActions();
  const others = useGwKeyActions();
  const [secret, setSecret] = useState<{ title: string; value: string }>();
  const [assigning, setAssigning] = useState<GwKey>();
  const own = myQ.data ?? null;
  // 能给 Key 分配权限的人：管理员，或至少是一个私有服务的所有者
  const canAssign = me.can_manage || me.owns_all || me.owned_services.length > 0;
  const rows = useMemo(() => {
    const list = me.can_manage ? (othersQ.data ?? []) : own ? [own] : [];
    return own ? [own, ...list.filter((k) => k.id !== own.id)] : list;
  }, [me.can_manage, othersQ.data, own]);
  const services = usable(servicesQ.data ?? []);

  const show = (title: string) => (k: GwKeySecret) => setSecret({ title, value: k.secret });
  const fail = (e: Error) => message.error(e.message);
  const apply = () =>
    modal.confirm({
      title: "申请用户 Key",
      okText: "申请",
      content:
        "申请后马上能调用公开服务；私有服务要由服务所有者或管理员给这把 Key 分配权限。申请时网关在用户中心为这把 Key 登记一个主体（不能登录、没有部门），之后的授权都授给它。",
      onOk: () => mine.apply.mutateAsync().then(show("已申请用户 Key"), fail),
    });
  const reset = (k: GwKey) =>
    modal.confirm({
      title: "重置用户 Key",
      okText: "重置",
      content: `生成新的原文，旧 Key ${k.masked} 立即失效，用旧 Key 的外部客户端要换成新的。主体和分配的权限不变。`,
      onOk: () => mine.reset.mutateAsync().then(show("已重置用户 Key"), fail),
    });
  const block = (k: GwKey) =>
    modal.confirm({
      title: "停用 Key",
      okText: "停用",
      okButtonProps: { danger: true },
      content: "停用后网关拒绝这把 Key 的所有调用（key_invalid）。授权保留，可以随时启用。",
      onOk: () =>
        (k.id === own?.id ? mine.setBlocked.mutateAsync(true) : others.act.mutateAsync({ id: k.id, action: "block" })).then(
          () => message.success("已停用"),
          fail,
        ),
    });
  const unblock = (k: GwKey) =>
    (k.id === own?.id ? mine.setBlocked.mutateAsync(false) : others.act.mutateAsync({ id: k.id, action: "unblock" })).then(
      () => message.success("已启用"),
      fail,
    );
  const del = (k: GwKey) =>
    modal.confirm({
      title: "删除用户 Key",
      okText: "删除",
      okButtonProps: { danger: true },
      content: `删除你的用户 Key ${k.masked}，不可恢复；用户中心里的主体停用，它的授权随之失效。重新申请后要重新分配权限。只是临时不用，请选「停用」。`,
      onOk: () => mine.remove.mutateAsync().then(() => message.success("已删除"), fail),
    });

  return (
    <>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="每人一把用户 Key，只能为本人申请，给 Claude Code、Cursor 这类外部客户端用。申请后马上能调用公开服务；私有服务要由服务所有者或管理员给这把 Key 分配权限（可以细到工具）。"
      />
      {myQ.error && <Alert type="error" showIcon style={{ marginBottom: 12 }} message={myQ.error.message} />}
      {!own && !myQ.isPending && (
        <div className={pageStyles.filterRow}>
          <span className={styles.muted}>{atlasMe.data?.name ?? "你"}还没有用户 Key</span>
          <Button type="primary" icon={<Icon name="plus" size={14} />} loading={mine.apply.isPending} onClick={apply}>
            申请用户 Key
          </Button>
        </div>
      )}
      <Table<GwKey>
        size="middle"
        rowKey="id"
        loading={myQ.isPending || (me.can_manage && othersQ.isPending)}
        dataSource={rows}
        pagination={false}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有用户 Key" /> }}
        columns={[
          {
            title: "持有人",
            render: (_, k) => (
              <span>
                <b>{k.name}</b> {k.id === own?.id && <Tag color="blue">我</Tag>}
              </span>
            ),
          },
          { title: "Key", width: 170, render: (_, k) => <KeyMasked k={k} /> },
          { title: "私有服务权限", render: (_, k) => <KeyScopes k={k} /> },
          { title: "状态", width: 90, render: (_, k) => <KeyStatusTag k={k} /> },
          { title: "申请时间", width: 140, render: (_, k) => fmtTime(k.created_at) },
          {
            title: "操作",
            width: 240,
            render: (_, k) => {
              const isMine = k.id === own?.id;
              return (
                <Space size={0}>
                  {canAssign && (
                    <Button size="small" type="link" onClick={() => setAssigning(k)}>
                      分配权限
                    </Button>
                  )}
                  {isMine && (
                    <Button size="small" type="link" onClick={() => reset(k)}>
                      重置
                    </Button>
                  )}
                  {k.status === "active" ? (
                    <Button size="small" type="link" onClick={() => block(k)}>
                      停用
                    </Button>
                  ) : (
                    <Button size="small" type="link" onClick={() => void unblock(k)}>
                      启用
                    </Button>
                  )}
                  {isMine && (
                    <Button size="small" type="link" danger onClick={() => del(k)}>
                      删除
                    </Button>
                  )}
                </Space>
              );
            },
          },
        ]}
      />
      {own && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 24, marginTop: 16 }}>
          <div>
            <div className={styles.secTitle}>
              我的 Key 能用的服务和工具 <span className={styles.muted}>变化最长 30 秒生效</span>
            </div>
            <Table<GwService>
              size="small"
              rowKey="name"
              loading={servicesQ.isPending}
              dataSource={services}
              pagination={false}
              locale={{ emptyText: "没有能用的服务" }}
              columns={[
                {
                  title: "服务",
                  render: (_, s) => (
                    <span>
                      {s.display_name} <code className={styles.muted}>{s.name}</code>
                    </span>
                  ),
                },
                {
                  title: "能用的工具",
                  render: (_, s) =>
                    s.my.scope === "tools" ? (
                      s.my.tools.map((t) => (
                        <Tag key={t} style={{ fontFamily: "var(--mono)" }}>
                          {t}
                        </Tag>
                      ))
                    ) : (
                      `全部 ${s.active_tool_count} 个`
                    ),
                },
                {
                  title: "来源",
                  width: 90,
                  render: (_, s) => ({ public: "公开", all: "服务级", tools: "工具级" })[s.my.scope as "public" | "all" | "tools"],
                },
              ]}
            />
          </div>
          <div>
            <div className={styles.secTitle}>
              在外部客户端里用 <span className={styles.muted}>Claude Code、Cursor 等</span>
            </div>
            <pre className={styles.code}>{clientConfig(me, services.slice(0, 2).map((s) => s.name))}</pre>
            <div className={styles.muted} style={{ fontSize: 12 }}>
              每个服务一个地址；tools/list 只返回这把 Key 有权限的工具。
            </div>
          </div>
        </div>
      )}
      {assigning && <AssignModal k={assigning} onClose={() => setAssigning(undefined)} />}
      {secret && (
        <Modal
          open
          width={640}
          title={secret.title}
          onCancel={() => setSecret(undefined)}
          footer={
            <Button type="primary" onClick={() => setSecret(undefined)}>
              我已保存
            </Button>
          }
        >
          {/* antd 的 copyable 在 http 下会回落到 execCommand（112 不是 HTTPS，浏览器的剪贴板接口不可用） */}
          <Typography.Paragraph copyable={{ text: secret.value }} className={styles.code} style={{ marginBottom: 0 }}>
            {secret.value}
          </Typography.Paragraph>
          <Alert type="warning" showIcon style={{ marginTop: 12 }} message="Key 只显示这一次，关闭后无法再查看；忘了可以「重置」（主体和权限不变）。" />
          <div className={styles.secTitle}>在外部客户端里用</div>
          <pre className={styles.code}>{clientConfig(me, services.slice(0, 2).map((s) => s.name), secret.value)}</pre>
        </Modal>
      )}
    </>
  );
}

// ═══════════════════════════════════════════════ 智能体 Key（管理员）

function AgentKeys({ keys, loading, error }: { keys: GwKey[]; loading: boolean; error: Error | null }) {
  const { modal, message } = AntdApp.useApp();
  const { act, remove } = useGwKeyActions();
  const [status, setStatus] = useState<string>();
  const [q, setQ] = useState("");
  const [assigning, setAssigning] = useState<GwKey>();
  const [creating, setCreating] = useState(false);
  const title = (k: GwKey) => k.name || k.client_code || "";
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return keys.filter(
      (k) =>
        (!status || k.status === status) &&
        (!needle || `${k.client_code ?? ""} ${k.name}`.toLowerCase().includes(needle)),
    );
  }, [keys, status, q]);
  const fail = (e: Error) => message.error(e.message);

  const reset = (k: GwKey) =>
    modal.confirm({
      title: "重置智能体 Key",
      okText: "重置",
      content: `为「${title(k)}」生成新的原文，旧 Key ${k.masked} 立即失效。Atlas 下一次获取时拿到新的；正在用旧 Key 的调用会失败一次后自动重取。主体和权限不变。`,
      onOk: () => act.mutateAsync({ id: k.id, action: "reset" }).then(() => message.success("已重置"), fail),
    });
  const block = (k: GwKey) =>
    modal.confirm({
      title: "停用智能体 Key",
      okText: "停用",
      okButtonProps: { danger: true },
      content: `停用后网关拒绝「${title(k)}」的所有调用（key_invalid），这个智能体在 Atlas 里调不了 MCP 工具。授权保留，可以随时启用。`,
      onOk: () => act.mutateAsync({ id: k.id, action: "block" }).then(() => message.success("已停用"), fail),
    });
  const del = (k: GwKey) =>
    modal.confirm({
      title: "删除智能体 Key",
      okText: "删除",
      okButtonProps: { danger: true },
      content: `删除「${title(k)}」的 Key（${k.masked}），不可恢复；用户中心里的主体停用，授权随之失效。Atlas 下次用到这个智能体时会重新申请一把（只能调公开服务，要重新分配权限）。只是临时不用，请选「停用」。`,
      onOk: () => remove.mutateAsync(k.id).then(() => message.success("已删除"), fail),
    });

  return (
    <>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="一个智能体一把 Key（客户端类型 Agent，客户端编码 = 智能体 id）。Atlas 新建智能体时调接口申请、删除智能体时调接口删除；还没有 Key 的智能体可以在这里补申请。Atlas 编辑、运行智能体时按类型 + 编码取 Key 和能用的工具，和智能体的权限类型无关。原文不在页面上显示。"
      />
      {error && <Alert type="error" showIcon style={{ marginBottom: 12 }} message={error.message} />}
      <div className={pageStyles.filterRow}>
        <Select
          allowClear
          placeholder="全部状态"
          style={{ width: 130 }}
          value={status}
          onChange={setStatus}
          options={Object.entries(GW_KEY_STATUS).map(([value, s]) => ({ value, label: s.label }))}
        />
        <Input allowClear placeholder="搜索智能体 id / 名称" style={{ width: 240 }} value={q} onChange={(e) => setQ(e.target.value)} />
        <Button type="primary" style={{ marginLeft: "auto" }} icon={<Icon name="plus" size={14} />} onClick={() => setCreating(true)}>
          为智能体申请 Key
        </Button>
      </div>
      <Table<GwKey>
        size="middle"
        rowKey="id"
        loading={loading}
        dataSource={rows}
        pagination={false}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有符合条件的智能体 Key" /> }}
        columns={[
          {
            title: "智能体",
            render: (_, k) => (
              <div>
                <b>{k.name}</b>
                <div className={styles.desc2} style={{ fontFamily: "var(--mono)" }}>
                  {GW_CLIENT_TYPE[k.client_type ?? ""] ?? k.client_type} · {k.client_code}
                </div>
              </div>
            ),
          },
          { title: "Key", width: 170, render: (_, k) => <KeyMasked k={k} /> },
          { title: "私有服务权限", render: (_, k) => <KeyScopes k={k} /> },
          { title: "状态", width: 90, render: (_, k) => <KeyStatusTag k={k} /> },
          { title: "申请时间", width: 140, render: (_, k) => fmtTime(k.created_at) },
          {
            title: "操作",
            width: 240,
            render: (_, k) => (
              <Space size={0}>
                <Button size="small" type="link" onClick={() => setAssigning(k)}>
                  分配权限
                </Button>
                <Button size="small" type="link" onClick={() => reset(k)}>
                  重置
                </Button>
                {k.status === "active" ? (
                  <Button size="small" type="link" onClick={() => block(k)}>
                    停用
                  </Button>
                ) : (
                  <Button
                    size="small"
                    type="link"
                    onClick={() =>
                      act.mutate({ id: k.id, action: "unblock" }, { onSuccess: () => message.success("已启用"), onError: fail })
                    }
                  >
                    启用
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
      {assigning && <AssignModal k={assigning} onClose={() => setAssigning(undefined)} />}
      {creating && (
        <NewAgentKeyModal
          taken={new Set(keys.map((k) => k.client_code ?? ""))}
          onClose={() => setCreating(false)}
          onCreated={(k) => {
            setCreating(false);
            setAssigning(k);
          }}
        />
      )}
    </>
  );
}

/** 给还没有 Key 的智能体补申请（例如接入前就建好的）。列表来自 Atlas */
function NewAgentKeyModal({
  taken,
  onClose,
  onCreated,
}: {
  taken: Set<string>;
  onClose: () => void;
  onCreated: (k: GwKey) => void;
}) {
  const { message } = AntdApp.useApp();
  const agentsQ = useAgents({ status: "all", limit: 200 });
  const { createClient } = useGwKeyActions();
  const [agentId, setAgentId] = useState<string>();
  const free = (agentsQ.data?.data ?? []).filter((a) => !taken.has(a.id));
  const agent = free.find((a) => a.id === agentId);

  return (
    <Modal
      open
      title="为智能体申请 Key"
      okText="申请并分配权限"
      okButtonProps={{ disabled: !agent, loading: createClient.isPending }}
      onCancel={onClose}
      onOk={() =>
        agent &&
        createClient.mutate(
          { client_type: "Agent", client_code: agent.id, name: agent.name },
          {
            onSuccess: (k) => {
              message.success(`已为「${agent.name}」申请 Key`);
              onCreated(k);
            },
            onError: (e) => message.error(e.message),
          },
        )
      }
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="Atlas 新建智能体时会自动申请。这里给还没有 Key 的智能体补申请；申请人成为用户中心里这个主体的负责人。"
      />
      <FormField label="智能体" hint="列表来自 Atlas；一个智能体只有一把 Key（按客户端类型 + 编码唯一）">
        <Select
          showSearch
          style={{ width: "100%" }}
          loading={agentsQ.isPending}
          value={agentId}
          onChange={setAgentId}
          optionFilterProp="label"
          placeholder={free.length || agentsQ.isPending ? "选择智能体" : "所有智能体都已经有 Key 了"}
          options={free.map((a) => ({ value: a.id, label: `${a.name}（${a.id}）` }))}
        />
      </FormField>
    </Modal>
  );
}
