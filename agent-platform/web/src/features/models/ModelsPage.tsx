"use client";

/**
 * 模型列表：普通用户只看到自己能用的（公有的、授权给自己或所在部门的）；管理员看全部，
 * 多「添加模型」、线路和状态、行操作。调用方只传模型名，网关按线路转给对应的账号。
 */
import { Alert, App as AntdApp, Button, Drawer, Empty, Input, Segmented, Skeleton, Space, Table, Tabs, Tag } from "antd";
import { type ReactNode, useMemo, useState } from "react";

import { MG_HEALTH, MG_MODEL_STATUS, type MgMe, type MgModel, useMgMe, useMgModelActions, useMgModels } from "@/api/model-gateway";
import { Icon } from "@/components/Icon";
import pageStyles from "@/features/agents/agents.module.css";
import styles from "@/features/catalog/catalog.module.css";
import { GrantsModal, ModelFormModal } from "./ModelForms";
import { FORMAT_HINT, fmtTokens, grantLabel, priceText, snippet, type SnippetKind } from "./shared";

/** 模型各页共用：先拿到「我」在模型平台上的身份，拿不到就整页报错 */
export function MgGate({ children }: { children: (me: MgMe) => ReactNode }) {
  const meQ = useMgMe();
  if (meQ.error) {
    return (
      <div className={pageStyles.body}>
        <Alert type="error" showIcon message="暂时连不上模型服务" description={meQ.error.message} />
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

export function ModelsPage() {
  return <MgGate>{(me) => <ModelsList manage={me.can_manage} gatewayUrl={me.gateway_url} />}</MgGate>;
}

const KIND: Record<MgModel["kind"], string> = { chat: "对话", embedding: "向量", rerank: "重排" };

// ═══════════════════════════════════════════════ 模型列表

function ModelsList({ manage, gatewayUrl }: { manage: boolean; gatewayUrl: string | null }) {
  const { modal, message } = AntdApp.useApp();
  const modelsQ = useMgModels();
  const { toggle, remove } = useMgModelActions();
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState("all");
  const [detail, setDetail] = useState<string>();
  const [form, setForm] = useState<{ model?: MgModel }>();
  const [granting, setGranting] = useState<string>();

  const models = useMemo(() => modelsQ.data ?? [], [modelsQ.data]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return models.filter(
      (m) =>
        (!needle || m.name.toLowerCase().includes(needle) || m.display_name.toLowerCase().includes(needle)) &&
        (filter === "all" || (filter === "off" ? !m.enabled : m.enabled && m.visibility === filter)),
    );
  }, [models, q, filter]);
  const issues = manage ? models.filter((m) => m.enabled && (m.status === "degraded" || m.status === "unavailable")) : [];
  const current = models.find((m) => m.id === detail);
  const grantModel = models.find((m) => m.id === granting);

  const confirmDelete = (m: MgModel) =>
    modal.confirm({
      title: `删除模型 ${m.name}`,
      okText: "删除",
      okButtonProps: { danger: true },
      content: `删除后 ${m.name} 立即不能调用，线路和授权一起删除，${m.keys ? `${m.keys} 把 Key 会去掉它` : "没有 Key 在用它"}。只是暂时不让用，请选「停用」。`,
      onOk: () =>
        remove.mutateAsync(m.id).then(
          (r) => {
            message.success(`已删除 ${m.name}${r.keys_updated ? `，更新了 ${r.keys_updated} 把 Key` : ""}`);
            setDetail(undefined);
          },
          (e: Error) => message.error(e.message),
        ),
    });
  const setEnabled = (m: MgModel, enabled: boolean) =>
    toggle.mutate(
      { id: m.id, enabled },
      { onSuccess: () => message.success(enabled ? "已启用" : "已停用"), onError: (e) => message.error(e.message) },
    );

  const ops = (m: MgModel) => (
    <Space size={0}>
      <Button size="small" type="link" onClick={() => setForm({ model: m })}>
        编辑
      </Button>
      {m.visibility === "private" && (
        <Button size="small" type="link" onClick={() => setGranting(m.id)}>
          授权
        </Button>
      )}
      <Button size="small" type="link" onClick={() => setEnabled(m, !m.enabled)}>
        {m.enabled ? "停用" : "启用"}
      </Button>
      <Button size="small" type="link" danger onClick={() => confirmDelete(m)}>
        删除
      </Button>
    </Space>
  );

  return (
    <div className={pageStyles.page}>
      <header className={pageStyles.head}>
        <div className={pageStyles.titleRow}>
          <h1 className={pageStyles.title}>模型</h1>
          {manage && (
            <Button type="primary" className={pageStyles.action} icon={<Icon name="plus" size={14} />} onClick={() => setForm({})}>
              添加模型
            </Button>
          )}
        </div>
        <div className={pageStyles.toolbar}>
          <Input
            allowClear
            style={{ width: 240 }}
            prefix={<Icon name="search" size={14} style={{ color: "var(--fg-4)" }} />}
            placeholder="搜索模型名、显示名称"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          {manage && (
            <Segmented
              value={filter}
              onChange={(v) => setFilter(v as string)}
              options={[
                { value: "all", label: "全部" },
                { value: "public", label: "公有" },
                { value: "private", label: "私有" },
                { value: "off", label: "已停用" },
              ]}
            />
          )}
        </div>
      </header>
      <div className={pageStyles.body}>
        {issues.length > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={issues.map((m) => `${m.display_name}：${MG_MODEL_STATUS[m.status].hint}`).join("；")}
          />
        )}
        {modelsQ.error && <Alert type="error" showIcon style={{ marginBottom: 12 }} message={modelsQ.error.message} />}
        <Table<MgModel>
          size="middle"
          rowKey="id"
          loading={modelsQ.isPending}
          dataSource={rows}
          pagination={false}
          locale={{
            emptyText: (
              <Empty
                description={q ? "没有找到，换个关键词试试" : manage ? "还没有模型：先在「账号」里登记供应商账号，再添加模型" : "还没有你能用的模型"}
                image={Empty.PRESENTED_IMAGE_SIMPLE}
              />
            ),
          }}
          onRow={(m) => ({ onClick: () => setDetail(m.id), style: { cursor: "pointer" } })}
          columns={[
            { title: "模型", render: (_, m) => <ModelName model={m} /> },
            ...(manage ? [{ title: "线路", width: 200, render: (_: unknown, m: MgModel) => <RouteChips model={m} /> }] : []),
            { title: "上下文", width: 90, render: (_, m) => fmtTokens(m.caps.context_window) },
            { title: "价格（元 / 百万 token，输入 / 输出）", width: 230, render: (_, m) => priceText(m.price) },
            {
              title: "谁能用",
              width: 200,
              render: (_: unknown, m: MgModel) => (manage ? <AccessCell model={m} /> : m.access === "public" ? "所有人" : "授权给你"),
            },
            ...(manage ? [{ title: "状态", width: 120, render: (_: unknown, m: MgModel) => <StatusTag model={m} /> }] : []),
          ]}
        />
      </div>

      <Drawer
        open={!!current}
        width={640}
        onClose={() => setDetail(undefined)}
        title={current ? <DrawerTitle model={current} /> : null}
        extra={current && manage ? ops(current) : null}
      >
        {current && <ModelDetail model={current} manage={manage} gatewayUrl={gatewayUrl} />}
      </Drawer>
      {form && <ModelFormModal model={form.model} models={models} onClose={() => setForm(undefined)} />}
      {grantModel && <GrantsModal model={grantModel} onClose={() => setGranting(undefined)} />}
    </div>
  );
}

function ModelName({ model: m }: { model: MgModel }) {
  return (
    <div>
      <b>{m.display_name}</b> {m.kind !== "chat" && <Tag>{KIND[m.kind]}</Tag>}
      {m.caps.vision && <Tag color="blue">看图</Tag>}
      <div className={styles.desc2} style={{ fontFamily: "var(--mono)" }}>
        {m.name}
      </div>
    </div>
  );
}

function RouteChips({ model: m }: { model: MgModel }) {
  if (!m.routes.length) return <span className={styles.muted}>—</span>;
  const accounts = [...new Map(m.routes.map((r) => [r.account_id, r])).values()];
  return (
    <span>
      {accounts.map((r) => (
        <Tag key={r.account_id} color={r.status === "ok" ? undefined : MG_HEALTH[r.status].color} title={MG_HEALTH[r.status].label}>
          {r.account_name}
        </Tag>
      ))}
    </span>
  );
}

function StatusTag({ model: m }: { model: MgModel }) {
  const s = MG_MODEL_STATUS[m.status];
  return (
    <Tag color={s.color} title={s.hint || undefined}>
      {s.label}
    </Tag>
  );
}

function AccessCell({ model: m }: { model: MgModel }) {
  if (m.visibility === "public") return <span>所有人</span>;
  const grants = m.grants ?? [];
  if (!grants.length)
    return (
      <Tag color="warning" title="除管理员外没人能用">
        私有 · 还没授权
      </Tag>
    );
  const labels = grants.map(grantLabel);
  return (
    <span>
      <Tag>私有</Tag>
      {labels.slice(0, 2).join("、")}
      {labels.length > 2 && <span className={styles.muted}> 等 {labels.length} 项</span>}
    </span>
  );
}

function DrawerTitle({ model: m }: { model: MgModel }) {
  return (
    <div>
      <div>
        {m.display_name} {m.visibility === "public" ? <Tag color="blue">公有</Tag> : <Tag>私有</Tag>}
        <StatusTag model={m} />
      </div>
      <div className={styles.sub} style={{ fontFamily: "var(--mono)", fontWeight: 400 }}>
        {m.name}
      </div>
    </div>
  );
}

function ModelDetail({ model: m, manage, gatewayUrl }: { model: MgModel; manage: boolean; gatewayUrl: string | null }) {
  const items = [
    ...(manage ? [{ key: "routes", label: `线路 ${m.routes.length}`, children: <RoutesTab model={m} /> }] : []),
    { key: "access", label: "谁能用", children: <AccessTab model={m} manage={manage} /> },
    { key: "spec", label: "规格与价格", children: <SpecTab model={m} /> },
    { key: "code", label: "调用示例", children: <CodeTab model={m} gatewayUrl={gatewayUrl} /> },
  ];
  return (
    <>
      {m.description && <p style={{ marginBottom: 8 }}>{m.description}</p>}
      {!m.enabled && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="已停用：调用会返回「模型已停用」。Key 里还留着它，重新启用后不用重新分配。"
        />
      )}
      <Tabs items={items} />
    </>
  );
}

function RoutesTab({ model: m }: { model: MgModel }) {
  const firstPriority = Math.min(...m.routes.map((r) => r.priority));
  return (
    <>
      <p className={styles.muted} style={{ marginBottom: 8 }}>
        优先级数字小的先用；同一优先级按权重分流，同一个会话尽量留在同一条线路上。线路出错（限流、过载、欠费）会自动换下一条。
      </p>
      <Table
        size="small"
        rowKey="id"
        pagination={false}
        dataSource={m.routes}
        columns={[
          {
            title: "优先级",
            width: 80,
            render: (_, r) => (
              <span>
                {r.priority} <span className={styles.muted}>{r.priority === firstPriority ? "首选" : "备用"}</span>
              </span>
            ),
          },
          { title: "账号", render: (_, r) => r.account_name },
          {
            title: "上游模型",
            render: (_, r) => (
              <span style={{ fontFamily: "var(--mono)" }}>
                {r.upstream_model}
                {r.upstream_model !== m.name && (
                  <Tag style={{ marginLeft: 6 }} title="和模型名不同，网关转发时换成它">
                    ≠ 模型名
                  </Tag>
                )}
              </span>
            ),
          },
          { title: "权重", width: 60, render: (_, r) => r.weight },
          { title: "状态", width: 90, render: (_, r) => <Tag color={MG_HEALTH[r.status].color}>{MG_HEALTH[r.status].label}</Tag> },
        ]}
      />
    </>
  );
}

function AccessTab({ model: m, manage }: { model: MgModel; manage: boolean }) {
  if (m.visibility === "public") return <p>所有人都能用：在「我的 Key」里选上就行，智能体也能选它。</p>;
  if (!manage) return <p>已授权给你或你所在的部门。</p>;
  const grants = m.grants ?? [];
  return grants.length ? (
    <div>
      {grants.map((g) => (
        <Tag key={g.id}>{grantLabel(g)}</Tag>
      ))}
      <p className={styles.muted} style={{ marginTop: 8 }}>
        {m.keys} 把 Key 选了它。收回授权后，对应的人的 Key 里它会失效。
      </p>
    </div>
  ) : (
    <p className={styles.muted}>还没有授权，除管理员外没人能用。</p>
  );
}

const THINKING: Record<MgModel["caps"]["thinking"], string> = { none: "不支持", optional: "可开可关", always_on: "一直开着" };

function SpecTab({ model: m }: { model: MgModel }) {
  const c = m.caps;
  return (
    <>
      <Kv k="类型" v={KIND[m.kind]} />
      <Kv k="上下文窗口" v={fmtTokens(c.context_window)} />
      <Kv k="最大输出" v={fmtTokens(c.max_output)} />
      {m.kind === "chat" && (
        <>
          <Kv k="看图" v={c.vision ? "支持（开了浏览器的智能体还能用 computer use）" : "不支持"} />
          <Kv k="工具调用" v={c.tools ? "支持" : "不支持"} />
          <Kv k="思考" v={THINKING[c.thinking]} />
        </>
      )}
      <Kv k="价格" v={`${priceText(m.price)}（元 / 百万 token，输入 / 输出）`} />
      <p className={styles.muted} style={{ marginTop: 8 }}>
        超出能力的请求（比如给不能看图的模型发图片）网关直接拒绝；思考、温度这类参数按上游的规则整理后再发。
      </p>
    </>
  );
}

function CodeTab({ model: m, gatewayUrl }: { model: MgModel; gatewayUrl: string | null }) {
  const [kind, setKind] = useState<SnippetKind>(m.kind === "chat" ? "claude-code" : "openai");
  return (
    <>
      <Segmented
        value={kind}
        onChange={(v) => setKind(v as typeof kind)}
        style={{ marginBottom: 8 }}
        options={[
          { value: "claude-code", label: "Claude Code" },
          { value: "universal", label: "通用接口" },
          { value: "anthropic", label: "Anthropic 格式" },
          { value: "openai", label: "OpenAI 格式" },
        ]}
      />
      <pre className={styles.code}>{snippet(kind, gatewayUrl, m.name)}</pre>
      <p className={styles.muted} style={{ marginTop: 8 }}>
        把 $MODEL_API_KEY 换成你的 Key（在「我的 Key」里新建），几种接口用同一把 Key。{FORMAT_HINT}
      </p>
    </>
  );
}

export function Kv({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className={styles.kv}>
      <span>{k}</span>
      <span>{v}</span>
    </div>
  );
}
