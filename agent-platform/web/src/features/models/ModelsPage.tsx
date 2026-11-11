"use client";

/**
 * 模型列表：普通用户只看到自己能用的（公有的、授权给自己或所在部门的）；管理员多「添加模型」和行操作。
 * 价格、上下文直接在列表里。Key 拆到「我的 Key」「客户端 Key」两个页面（KeysTab.tsx）。
 */
import { Alert, App as AntdApp, Button, Drawer, Empty, Input, Segmented, Select, Skeleton, Space, Table, Tag } from "antd";
import { type ReactNode, useMemo, useState } from "react";

import { type MgMe, type MgModel, type MgProvider, useDeleteMgModel, useMgMe, useMgModels, useMgProviders } from "@/api/model-gateway";
import { Icon } from "@/components/Icon";
import pageStyles from "@/features/agents/agents.module.css";
import styles from "@/features/catalog/catalog.module.css";
import { GrantsModal, type FormKind, ModelFormModal } from "./ModelForms";
import { curlFor, fmtPrice, fmtTokens, grantLabel } from "./shared";

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
  return <MgGate>{(me) => <ModelsList manage={me.can_manage} litellmUrl={me.litellm_url ?? null} />}</MgGate>;
}

// ═══════════════════════════════════════════════ 模型列表

function ModelsList({ manage, litellmUrl }: { manage: boolean; litellmUrl: string | null }) {
  const { modal, message } = AntdApp.useApp();
  const modelsQ = useMgModels();
  const providersQ = useMgProviders(true);
  const remove = useDeleteMgModel();
  const [q, setQ] = useState("");
  const [provider, setProvider] = useState("");
  const [vis, setVis] = useState<string>();
  const [source, setSource] = useState<string>();
  const [detail, setDetail] = useState<string>();
  const [form, setForm] = useState<{ kind: FormKind; model?: MgModel }>();
  const [granting, setGranting] = useState<string>();

  const models = modelsQ.data?.data ?? [];
  const providers = providersQ.data ?? [];
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return models.filter(
      (m) =>
        (!needle || m.name.toLowerCase().includes(needle) || m.display_name.toLowerCase().includes(needle)) &&
        (!provider || m.provider === provider) &&
        (!vis || m.visibility === vis) &&
        (!source || m.source === source),
    );
  }, [models, q, provider, vis, source]);
  // 只列出现过的提供商，按模型数排
  const providerTabs = useMemo(() => {
    const count = new Map<string, number>();
    for (const m of models) if (m.provider) count.set(m.provider, (count.get(m.provider) ?? 0) + 1);
    return [...count.entries()].sort((a, b) => b[1] - a[1]).map(([code]) => code);
  }, [models]);
  const current = models.find((m) => rowKey(m) === detail);
  const grantModel = models.find((m) => m.uuid === granting);

  const confirmDelete = (m: MgModel) =>
    modal.confirm({
      title: `删除模型 ${m.name}`,
      okText: "删除",
      okButtonProps: { danger: true },
      content:
        m.managed && m.status !== "missing"
          ? `删除后 ${m.name} 立即不能调用，授权一起删除，已分配它的 Key 会去掉它。`
          : "删除平台上的记录和授权，已分配它的 Key 会去掉它。模型服务里直接配置的保留。",
      onOk: () =>
        remove.mutateAsync(m.uuid!).then(
          (r) => {
            message.success(`已删除 ${m.name}${r.keys_updated ? `，更新了 ${r.keys_updated} 把 Key` : ""}`);
            setDetail(undefined);
          },
          (e: Error) => message.error(e.message),
        ),
    });

  const ops = (m: MgModel) => {
    if (m.source === "litellm")
      return (
        <Button size="small" type="link" onClick={() => setForm({ kind: "adopt", model: m })}>
          纳入平台
        </Button>
      );
    return (
      <Space size={0}>
        {m.visibility === "private" && (
          <Button size="small" type="link" onClick={() => setGranting(m.uuid!)}>
            授权
          </Button>
        )}
        {m.status === "missing" ? (
          <Button size="small" type="link" onClick={() => setForm({ kind: "reregister", model: m })}>
            重新登记
          </Button>
        ) : (
          <Button size="small" type="link" onClick={() => setForm({ kind: "edit", model: m })}>
            编辑
          </Button>
        )}
        <Button size="small" type="link" danger onClick={() => confirmDelete(m)}>
          删除
        </Button>
      </Space>
    );
  };

  return (
    <div className={pageStyles.page}>
      <header className={pageStyles.head}>
        <div className={pageStyles.titleRow}>
          <h1 className={pageStyles.title}>模型</h1>
          {manage && (
            <Button type="primary" className={pageStyles.action} icon={<Icon name="plus" size={14} />} onClick={() => setForm({ kind: "new" })}>
              添加模型
            </Button>
          )}
        </div>
        <div className={pageStyles.toolbar}>
          <Input
            allowClear
            style={{ width: 240 }}
            prefix={<Icon name="search" size={14} style={{ color: "var(--fg-4)" }} />}
            placeholder="搜索模型"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          {providerTabs.length > 1 && (
            <Segmented
              value={provider}
              onChange={(v) => setProvider(v as string)}
              options={[{ value: "", label: "全部" }, ...providerTabs.map((c) => ({ value: c, label: providerName(providers, c) }))]}
            />
          )}
          <span style={{ flex: 1 }} />
          {manage && (
            <>
              <Select
                allowClear
                placeholder="谁能用"
                style={{ width: 120 }}
                value={vis}
                onChange={setVis}
                options={[
                  { value: "public", label: "所有人" },
                  { value: "private", label: "需授权" },
                ]}
              />
              <Select
                allowClear
                placeholder="来源"
                style={{ width: 120 }}
                value={source}
                onChange={setSource}
                options={[
                  { value: "platform", label: "平台添加" },
                  { value: "litellm", label: "外部配置" },
                ]}
              />
            </>
          )}
        </div>
      </header>
      <div className={pageStyles.body}>
        {modelsQ.data && !modelsQ.data.litellm_reachable && (
          <Alert type="warning" showIcon style={{ marginBottom: 12 }} message="模型服务暂时连不上，模型是否可用暂时未知" />
        )}
        {modelsQ.error && <Alert type="error" showIcon style={{ marginBottom: 12 }} message={modelsQ.error.message} />}
        <Table<MgModel>
          size="middle"
          rowKey={rowKey}
          loading={modelsQ.isPending}
          dataSource={rows}
          pagination={false}
          locale={{
            emptyText: (
              <Empty description={q ? "没有找到，换个关键词试试" : manage ? "还没有模型" : "还没有你能用的模型"} image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ),
          }}
          onRow={(m) => ({ onClick: () => setDetail(rowKey(m)), style: { cursor: "pointer" } })}
          columns={[
            {
              title: "模型",
              render: (_, m) => (
                <div>
                  <b>{m.display_name}</b>
                  <div className={styles.desc2} style={{ fontFamily: "var(--mono)" }}>
                    {m.name}
                  </div>
                </div>
              ),
            },
            { title: "提供商", width: 130, render: (_, m) => providerName(providers, m.provider) },
            { title: "上下文", width: 100, render: (_, m) => fmtTokens(m.max_input_tokens) },
            { title: "价格（每百万 token）", width: 170, render: (_, m) => fmtPrice(m) },
            {
              title: "谁能用",
              width: 220,
              render: (_: unknown, m: MgModel) => (manage ? <AccessCell model={m} /> : m.access === "public" ? "所有人" : "授权给你"),
            },
            { title: "状态", width: 130, render: (_, m) => <StatusTag model={m} /> },
            ...(manage
              ? [
                  {
                    title: "操作",
                    width: 200,
                    render: (_: unknown, m: MgModel) => <span onClick={(e) => e.stopPropagation()}>{ops(m)}</span>,
                  },
                ]
              : []),
          ]}
        />
      </div>

      <Drawer
        open={!!current}
        width={560}
        onClose={() => setDetail(undefined)}
        title={current ? <DrawerTitle model={current} manage={manage} /> : null}
        extra={current && manage ? ops(current) : null}
      >
        {current && (
          <ModelDetail model={current} manage={manage} providers={providers} litellmUrl={litellmUrl} />
        )}
      </Drawer>
      {form && (
        <ModelFormModal
          kind={form.kind}
          model={form.model}
          models={models}
          providers={providers}
          onClose={() => setForm(undefined)}
        />
      )}
      {grantModel && <GrantsModal model={grantModel} onClose={() => setGranting(undefined)} />}
    </div>
  );
}

const rowKey = (m: MgModel) => m.uuid ?? `litellm:${m.name}`;

function providerName(providers: MgProvider[], code: string): string {
  return providers.find((p) => p.code === code)?.name ?? (code || "—");
}

function SourceTag({ model: m }: { model: MgModel }) {
  if (m.source === "platform") return null;
  return <Tag title="在模型服务里直接配置，纳入平台后才能使用">外部配置</Tag>;
}

function StatusTag({ model: m }: { model: MgModel }) {
  if (m.status === "missing")
    return (
      <Tag color="error" title="模型服务里找不到它了，调用会失败">
        已失效
      </Tag>
    );
  if (m.status === "unknown") return <Tag>状态未知</Tag>;
  return <Tag color="success">可用</Tag>;
}

function VisTag({ model: m }: { model: MgModel }) {
  return m.visibility === "public" ? <Tag color="blue">所有人</Tag> : <Tag>需授权</Tag>;
}

function AccessCell({ model: m }: { model: MgModel }) {
  if (m.source === "litellm")
    return (
      <span>
        <SourceTag model={m} />
        <span className={styles.muted}>纳入平台后才能使用</span>
      </span>
    );
  if (m.visibility === "public") return <span>所有人</span>;
  const grants = m.grants ?? [];
  if (!grants.length)
    return (
      <span>
        <VisTag model={m} />
        <Tag color="warning" title="除管理员外没人能用">
          还没授权给任何人
        </Tag>
      </span>
    );
  const labels = grants.map(grantLabel);
  return (
    <span>
      {labels.slice(0, 2).join("、")}
      {labels.length > 2 && <span className={styles.muted}> 等 {labels.length} 项</span>}
    </span>
  );
}

function DrawerTitle({ model: m, manage }: { model: MgModel; manage: boolean }) {
  return (
    <div>
      <div>
        {m.display_name} {m.visibility && <VisTag model={m} />}
        {manage && <SourceTag model={m} />}
        <StatusTag model={m} />
      </div>
      <div className={styles.sub} style={{ fontFamily: "var(--mono)", fontWeight: 400 }}>
        {m.name}
      </div>
    </div>
  );
}

function ModelDetail({
  model: m,
  manage,
  providers,
  litellmUrl,
}: {
  model: MgModel;
  manage: boolean;
  providers: MgProvider[];
  litellmUrl: string | null;
}) {
  const origin = m.source === "platform" ? (m.managed ? "平台添加" : "外部配置，已纳入平台") : "外部配置";
  return (
    <>
      {m.status === "missing" && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="这个模型已经失效"
          description="现在调用会失败。管理员可以重新添加（要重新填写 API Key），或删除它。"
        />
      )}
      {m.source === "platform" ? (
        <>
          <div className={styles.secTitle}>基本信息</div>
          <Kv k="名称" v={m.display_name} />
          <Kv k="提供商" v={providerName(providers, m.provider)} />
          <Kv k="谁能用" v={m.visibility === "public" ? "所有人" : "授权的人和部门"} />
          <Kv k="描述" v={m.description || "—"} />
          {m.created_at && <Kv k="添加时间" v={m.created_at.slice(0, 16).replace("T", " ")} />}
        </>
      ) : (
        <Alert
          type="info"
          showIcon
          message="这个模型在模型服务里直接配置，纳入平台后才能授权、放进 Key。"
        />
      )}
      {m.visibility === "private" && (
        <>
          <div className={styles.secTitle}>授权</div>
          {manage ? (
            (m.grants ?? []).length ? (
              <div>
                {(m.grants ?? []).map((g) => (
                  <Tag key={g.id}>{grantLabel(g)}</Tag>
                ))}
              </div>
            ) : (
              <p className={styles.muted}>还没有授权，除管理员外没人能用。</p>
            )
          ) : (
            <p>已授权给你或你所在的部门</p>
          )}
        </>
      )}
      {m.status !== "missing" && m.status !== "unknown" && (
        <>
          <div className={styles.secTitle}>规格</div>
          {manage && <Kv k="来源" v={origin} />}
          {manage && m.deployments > 1 && <Kv k="部署数" v={`${m.deployments}（自动负载均衡）`} />}
          {manage && <Kv k="接口地址" v={m.api_base || "提供商默认地址"} />}
          {manage && <Kv k="API Key" v="已加密保存" />}
          <Kv k="上下文窗口" v={fmtTokens(m.max_input_tokens)} />
          <Kv k="最大输出" v={fmtTokens(m.max_output_tokens)} />
          <Kv k="价格" v={`${fmtPrice(m)}（每百万 token，输入 / 输出）`} />
        </>
      )}
      <div className={styles.secTitle}>调用示例</div>
      <pre className={styles.code}>{curlFor(litellmUrl, m.name)}</pre>
      <p className={styles.muted} style={{ marginTop: 8 }}>
        把 $MODEL_API_KEY 换成你的 Key（在「我的 Key」里申请）。
      </p>
    </>
  );
}

function Kv({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className={styles.kv}>
      <span>{k}</span>
      <span>{v}</span>
    </div>
  );
}
