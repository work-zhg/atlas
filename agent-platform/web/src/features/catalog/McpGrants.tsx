"use client";

/**
 * 授权：授给 API Key（用户中心数据权限，授给 Key 的主体）；所有者是人。
 *
 *   服务级   数据编码 McpServer · 数据 服务名 → Key 主体 · READ：全部工具，含以后新增的
 *   工具级   数据编码 McpTool · 数据 服务名/工具名 → Key 主体 · READ：只能用这几个
 *   所有者   McpServer · 服务名 → 人 · OWNER：能给 Key 分配这个服务的权限、增删所有者；不能改连接和工具
 *
 * 两个入口：服务详情的「授权」（一个服务、多把 Key）；API Key 的「分配权限」（一把 Key、多个服务）。
 * 网关判权按 Key 缓存 30 秒：新授、收回最长 30 秒生效。
 */
import { Alert, App as AntdApp, Button, Checkbox, Empty, Modal, Radio, Select, Skeleton, Space, Table, Tag } from "antd";
import { useMemo, useState } from "react";

import {
  type GwKey,
  type GwKeyGrant,
  type GwKeyRef,
  type GwService,
  type GwServiceDetail,
  type GwServiceScope,
  type GwTool,
  GW_KEY_STATUS,
  useGwGrantActions,
  useGwGrants,
  useGwKeyActions,
  useGwKeyGrants,
  useGwKeyOptions,
  useGwService,
  useGwServices,
  useGwUsers,
} from "@/api/mcp-gateway";
import { Icon } from "@/components/Icon";
import styles from "./catalog.module.css";
import { EffectTag, FormField, KindTag } from "./McpForms";

type Scope = "all" | "tools";

function KeyCell({ k }: { k: GwKeyRef }) {
  return (
    <div>
      <b>{k.name || k.client_code}</b> <Tag>{k.key_type === "user" ? "用户 Key" : "智能体 Key"}</Tag>
      <div className={styles.desc2} style={{ fontFamily: "var(--mono)" }}>
        主体 {k.subject_uid}
      </div>
    </div>
  );
}

function ScopeCell({ scope, tools }: { scope: Scope; tools: string[] }) {
  if (scope === "all") return <span>全部工具 <span className={styles.muted}>含以后新增</span></span>;
  return (
    <span>
      {tools.map((t) => (
        <Tag key={t} style={{ fontFamily: "var(--mono)" }}>
          {t}
        </Tag>
      ))}
    </span>
  );
}

/** 选工具：服务的全部工具，停用的也能选（启用后生效） */
function ToolChecks({ tools, value, onChange }: { tools: GwTool[]; value: string[]; onChange: (v: string[]) => void }) {
  return (
    <Checkbox.Group
      style={{ display: "grid", gap: 6, maxHeight: 260, overflowY: "auto", marginTop: 8 }}
      value={value}
      onChange={(v) => onChange(v as string[])}
    >
      {tools.map((t) => (
        <Checkbox key={t.name} value={t.name}>
          <code>{t.name}</code> {t.effect !== "read" && <EffectTag effect={t.effect} />}
          {t.status === "disabled" && <Tag>已停用</Tag>}
          <span className={styles.muted} style={{ fontSize: 12 }}>
            {t.description}
          </span>
        </Checkbox>
      ))}
    </Checkbox.Group>
  );
}

// ═══════════════════════════════════════════════ 服务详情 ·「授权」

export function ServiceGrants({ service: s }: { service: GwServiceDetail }) {
  const { modal, message } = AntdApp.useApp();
  const grantsQ = useGwGrants(s.name, s.can_assign);
  const actions = useGwGrantActions(s.name);
  const [editing, setEditing] = useState<GwKeyGrant | "new">();
  const [addingOwner, setAddingOwner] = useState(false);
  const g = grantsQ.data;
  const pub = s.visibility === "public";

  if (grantsQ.error) return <Alert type="error" showIcon message={grantsQ.error.message} />;
  if (!g) return <Skeleton active paragraph={{ rows: 4 }} />;

  const revoke = (x: GwKeyGrant) =>
    modal.confirm({
      title: "收回授权",
      okText: "收回",
      okButtonProps: { danger: true },
      content: `收回「${x.key.name || x.key.client_code}」对「${s.display_name}」的权限？最长 30 秒后，带这把 Key 的调用开始被拒绝（forbidden）。`,
      onOk: () => actions.revoke.mutateAsync(x.key.id).then(() => message.success("已收回授权"), (e: Error) => message.error(e.message)),
    });
  const removeOwner = (userId: string, name: string) =>
    modal.confirm({
      title: "移除所有者",
      okText: "移除",
      okButtonProps: { danger: true },
      content: `移除 ${name} 的所有者权限？已经分配给 Key 的权限不受影响。`,
      onOk: () => actions.removeOwner.mutateAsync(userId).then(() => message.success("已移除"), (e: Error) => message.error(e.message)),
    });

  return (
    <>
      {pub ? (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="公开服务：任何有效的 API Key 都能调用全部工具，不需要授权。改成私有后，授给 Key 的权限才起作用。"
        />
      ) : (
        <>
          <div className={styles.secTitle}>
            授权的 API Key
            <span className={styles.muted}>服务级 = 全部工具（含以后新增的）；工具级 = 只能用指定的工具</span>
            <span style={{ flex: 1 }} />
            <Button size="small" type="primary" icon={<Icon name="plus" size={13} />} onClick={() => setEditing("new")}>
              授权 Key
            </Button>
          </div>
          <Table<GwKeyGrant>
            size="small"
            rowKey={(x) => x.key.id}
            dataSource={g.keys}
            pagination={false}
            locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有授权给任何 Key" /> }}
            columns={[
              { title: "API Key", render: (_, x) => <KeyCell k={x.key} /> },
              {
                title: "状态",
                width: 90,
                render: (_, x) => <Tag color={GW_KEY_STATUS[x.key.status].color}>{GW_KEY_STATUS[x.key.status].label}</Tag>,
              },
              { title: "范围", render: (_, x) => <ScopeCell scope={x.scope} tools={x.tools} /> },
              {
                title: "",
                width: 110,
                render: (_, x) => (
                  <Space size={0}>
                    <Button size="small" type="link" onClick={() => setEditing(x)}>
                      修改
                    </Button>
                    <Button size="small" type="link" danger onClick={() => revoke(x)}>
                      收回
                    </Button>
                  </Space>
                ),
              },
            ]}
          />
          <p className={styles.muted} style={{ fontSize: 12, marginTop: 8 }}>
            网关判权时以 Key 的主体作 X-Auth-Uid 调用户中心，结果按 Key 缓存 30 秒：新授、收回的授权最长 30 秒生效。
          </p>
        </>
      )}
      <div className={styles.secTitle}>
        所有者
        <span className={styles.muted}>能给 API Key 分配这个服务的权限；不能改连接和工具</span>
        <span style={{ flex: 1 }} />
        <Button size="small" onClick={() => setAddingOwner(true)}>
          ＋ 添加所有者
        </Button>
      </div>
      <Table
        size="small"
        rowKey="user_id"
        dataSource={g.owners}
        pagination={false}
        locale={{ emptyText: "没有所有者（只有 MCP 管理员能授权）" }}
        columns={[
          { title: "用户", dataIndex: "name", render: (v: string, o) => v || o.user_id },
          {
            title: "",
            width: 80,
            render: (_, o) => (
              <Button size="small" type="link" danger onClick={() => removeOwner(o.user_id, o.name || o.user_id)}>
                移除
              </Button>
            ),
          },
        ]}
      />
      <p className={styles.muted} style={{ fontSize: 12, marginTop: 8 }}>
        所有者是人（数据权限 McpServer = OWNER），登记人自动成为所有者。MCP 管理员有「全部 MCP 服务 · 所有者」（数据编码级授权），不在列表里。
      </p>
      {editing && <KeyGrantModal service={s} editing={editing === "new" ? null : editing} granted={g.keys} onClose={() => setEditing(undefined)} />}
      {addingOwner && <OwnerModal service={s} owners={g.owners.map((o) => o.user_id)} onClose={() => setAddingOwner(false)} />}
    </>
  );
}

function KeyGrantModal({
  service: s,
  editing,
  granted,
  onClose,
}: {
  service: GwServiceDetail;
  editing: GwKeyGrant | null;
  granted: GwKeyGrant[];
  onClose: () => void;
}) {
  const { message } = AntdApp.useApp();
  const optionsQ = useGwKeyOptions(!editing);
  const actions = useGwGrantActions(s.name);
  const [keyId, setKeyId] = useState<string | undefined>(editing?.key.id);
  const [scope, setScope] = useState<Scope>(editing?.scope ?? "all");
  const [tools, setTools] = useState<string[]>(editing?.tools ?? []);
  const old = granted.find((x) => x.key.id === keyId);
  const options = useMemo(() => {
    const keys = optionsQ.data ?? [];
    const opt = (k: GwKeyRef) => ({ value: k.id, label: `${k.name || k.client_code}${k.status === "blocked" ? "（已停用）" : ""}` });
    return [
      { label: "用户 Key", options: keys.filter((k) => k.key_type === "user").map(opt) },
      { label: "智能体 Key", options: keys.filter((k) => k.key_type === "client").map(opt) },
    ];
  }, [optionsQ.data]);

  return (
    <Modal
      open
      width={720}
      title={editing ? "修改授权" : `授权 Key · ${s.display_name}`}
      okText="保存"
      okButtonProps={{ disabled: !keyId || (scope === "tools" && !tools.length), loading: actions.set.isPending }}
      onCancel={onClose}
      onOk={() =>
        keyId &&
        actions.set.mutate(
          { key_id: keyId, scope, tools: scope === "tools" ? tools : [] },
          {
            onSuccess: () => {
              message.success("已授权，最长 30 秒生效");
              onClose();
            },
            onError: (e) => message.error(e.message),
          },
        )
      }
    >
      <FormField label="服务">
        <span>
          {s.display_name} <code>{s.name}</code>
        </span>
      </FormField>
      <FormField label="API Key" hint={!editing && old ? "这把 Key 已有授权，保存后替换原来的" : "授权授给 Key 在用户中心的主体"}>
        {editing ? (
          <KeyCell k={editing.key} />
        ) : (
          <Select
            style={{ width: "100%" }}
            loading={optionsQ.isPending}
            value={keyId}
            onChange={(id) => {
              setKeyId(id);
              const had = granted.find((x) => x.key.id === id);
              setScope(had?.scope ?? "all");
              setTools(had?.tools ?? []);
            }}
            options={options}
            placeholder="选择 Key"
            showSearch
            optionFilterProp="label"
          />
        )}
      </FormField>
      <FormField label="范围">
        <Radio.Group value={scope} onChange={(e) => setScope(e.target.value)}>
          <Radio value="all">全部工具（含以后新增的）</Radio>
          <Radio value="tools">指定工具</Radio>
        </Radio.Group>
        {scope === "tools" && <ToolChecks tools={s.tools} value={tools} onChange={setTools} />}
      </FormField>
    </Modal>
  );
}

function OwnerModal({ service: s, owners, onClose }: { service: GwServiceDetail; owners: string[]; onClose: () => void }) {
  const { message } = AntdApp.useApp();
  const actions = useGwGrantActions(s.name);
  const [q, setQ] = useState("");
  const [userId, setUserId] = useState<string>();
  const usersQ = useGwUsers(q);
  return (
    <Modal
      open
      title={`添加所有者 · ${s.display_name}`}
      okText="添加"
      okButtonProps={{ disabled: !userId, loading: actions.addOwner.isPending }}
      onCancel={onClose}
      onOk={() =>
        userId &&
        actions.addOwner.mutate(userId, {
          onSuccess: () => {
            message.success("已添加所有者");
            onClose();
          },
          onError: (e) => message.error(e.message),
        })
      }
    >
      <FormField label="用户" hint="所有者能给 API Key 分配这个服务的权限、增删所有者；改连接和工具仍然只有 MCP 管理员">
        <Select
          showSearch
          style={{ width: "100%" }}
          placeholder="输入姓名或账号搜索"
          filterOption={false}
          onSearch={setQ}
          value={userId}
          onChange={setUserId}
          loading={usersQ.isFetching}
          notFoundContent={q ? "没有找到" : null}
          options={(usersQ.data ?? [])
            .filter((u) => !owners.includes(u.id))
            .map((u) => ({ value: u.id, label: `${u.name}${u.account ? `（${u.account}）` : ""}` }))}
        />
      </FormField>
    </Modal>
  );
}

// ═══════════════════════════════════════════════ API Key ·「分配权限」

interface Draft {
  on: boolean;
  scope: Scope;
  tools: string[];
}

const sameDraft = (a: Draft, b: GwServiceScope | undefined) =>
  a.on ? !!b && a.scope === b.scope && (a.scope === "all" || [...a.tools].sort().join() === [...b.tools].sort().join()) : !b;

/** 一次给一把 Key 配多个私有服务；只能改自己是所有者的服务（MCP 管理员是全部） */
export function AssignModal({ k, onClose }: { k: GwKey; onClose: () => void }) {
  const { message } = AntdApp.useApp();
  const servicesQ = useGwServices();
  const currentQ = useGwKeyGrants(k.id);
  const { setGrants } = useGwKeyActions();
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const priv = (servicesQ.data ?? []).filter((s) => s.visibility === "private");
  const pubs = (servicesQ.data ?? []).filter((s) => s.visibility === "public" && s.enabled);
  const current = new Map((currentQ.data ?? []).map((g) => [g.service, g]));
  const draftOf = (s: GwService): Draft => {
    const g = current.get(s.name);
    return drafts[s.name] ?? { on: !!g, scope: g?.scope ?? "all", tools: g?.tools ?? [] };
  };
  const set = (s: GwService, patch: Partial<Draft>) => setDrafts({ ...drafts, [s.name]: { ...draftOf(s), ...patch } });

  const save = () => {
    const changed = priv.filter((s) => s.my.owner && drafts[s.name] && !sameDraft(draftOf(s), current.get(s.name)));
    const empty = changed.find((s) => draftOf(s).on && draftOf(s).scope === "tools" && !draftOf(s).tools.length);
    if (empty) {
      message.error(`「${empty.display_name}」至少选一个工具，或者取消勾选这个服务`);
      return;
    }
    if (!changed.length) {
      onClose();
      return;
    }
    setGrants.mutate(
      {
        id: k.id,
        grants: changed.map((s) => {
          const d = draftOf(s);
          return { service: s.name, scope: d.on ? d.scope : "none", tools: d.on && d.scope === "tools" ? d.tools : [] };
        }),
      },
      {
        onSuccess: () => {
          message.success(`已更新 ${changed.length} 个服务的权限，最长 30 秒生效`);
          onClose();
        },
        onError: (e) => message.error(e.message),
      },
    );
  };

  return (
    <Modal
      open
      width={760}
      title={`分配权限 · ${k.name || k.client_code}`}
      okText="保存"
      okButtonProps={{ loading: setGrants.isPending, disabled: currentQ.isPending }}
      onCancel={onClose}
      onOk={save}
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message={`公开服务不用分配，任何有效的 Key 都能调用：${pubs.map((s) => s.display_name).join("、") || "（没有）"}。`}
      />
      {(servicesQ.isPending || currentQ.isPending) && <Skeleton active paragraph={{ rows: 4 }} />}
      {currentQ.error && <Alert type="error" showIcon message={`取不到这把 Key 现在的权限：${currentQ.error.message}`} />}
      {currentQ.data && !priv.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有私有服务" />}
      {currentQ.data &&
        priv.map((s) => {
          const d = draftOf(s);
          const can = s.my.owner;
          return (
            <div key={s.name} style={{ borderBottom: "1px solid var(--line)", padding: "8px 0" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <Checkbox checked={d.on} disabled={!can} onChange={(e) => set(s, { on: e.target.checked })}>
                  <b>{s.display_name}</b>
                </Checkbox>
                <code className={styles.muted}>{s.name}</code>
                <KindTag kind={s.kind} />
                {!s.enabled && <Tag>已停用</Tag>}
                {!can && <span className={styles.muted} style={{ fontSize: 12 }}>你不是所有者，只能看</span>}
              </div>
              {d.on && (
                <div style={{ paddingLeft: 24, marginTop: 6 }}>
                  <Radio.Group value={d.scope} disabled={!can} onChange={(e) => set(s, { scope: e.target.value })}>
                    <Radio value="all">全部工具（含以后新增的）</Radio>
                    <Radio value="tools">指定工具</Radio>
                  </Radio.Group>
                  {d.scope === "tools" && <ServiceToolChecks name={s.name} disabled={!can} value={d.tools} onChange={(tools) => set(s, { tools })} />}
                </div>
              )}
            </div>
          );
        })}
      <p className={styles.muted} style={{ fontSize: 12, marginTop: 8 }}>
        写进用户中心，授给这把 Key 的主体；修改最长 30 秒生效，Key 本身不变。
      </p>
    </Modal>
  );
}

/** 列表接口不带工具：选「指定工具」时再取这个服务的详情 */
function ServiceToolChecks({
  name,
  disabled,
  value,
  onChange,
}: {
  name: string;
  disabled: boolean;
  value: string[];
  onChange: (v: string[]) => void;
}) {
  const detailQ = useGwService(name);
  if (!detailQ.data) return <Skeleton active paragraph={{ rows: 2 }} title={false} />;
  if (disabled)
    return (
      <div style={{ marginTop: 6 }}>
        {value.map((t) => (
          <Tag key={t} style={{ fontFamily: "var(--mono)" }}>
            {t}
          </Tag>
        ))}
      </div>
    );
  return <ToolChecks tools={detailQ.data.tools} value={value} onChange={onChange} />;
}

/** Key 列表里的「私有服务权限」 */
export function KeyScopes({ k }: { k: GwKey }) {
  if (!k.grants.length) return <span className={styles.muted}>只有公开服务</span>;
  return (
    <span>
      {k.grants.map((g) => (
        <Tag key={g.service} style={{ fontFamily: "var(--mono)" }} title={g.scope === "tools" ? g.tools.join("、") : "全部工具（含以后新增的）"}>
          {g.service} · {g.scope === "all" ? "全部" : `${g.tools.length} 个工具`}
        </Tag>
      ))}
    </span>
  );
}
