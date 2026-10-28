"use client";

/**
 * 模型管理（agent-platform/doc/design/model-design.html，原型 doc/prototype/model-management.html）。
 * 后端是模型管理平台；模型调用不经过它，调用日志、花费在 LiteLLM 管理界面看。
 *
 *   模型      平台登记的与 LiteLLM 里直接配置的合并展示。管理员登记、编辑、纳入、删除，给私有模型授权；
 *             普通用户只看到公有的和授权给自己（或所在部门）的
 *   API Key   我的用户 Key（所有人）；客户端 Key（管理员：应用创建的、平台服务用的）
 */
import { Alert, App as AntdApp, Button, Drawer, Empty, Input, Select, Space, Table, Tabs, Tag } from "antd";
import { useMemo, useState } from "react";

import { type MgModel, type MgProvider, useDeleteMgModel, useMgMe, useMgModels, useMgProviders } from "@/api/model-gateway";
import { Icon } from "@/components/Icon";
import pageStyles from "@/features/agents/agents.module.css";
import styles from "@/features/catalog/catalog.module.css";
import { KeysTab } from "./KeysTab";
import { GrantsModal, type FormKind, ModelFormModal } from "./ModelForms";
import { curlFor, fmtPrice, fmtTokens, grantLabel } from "./shared";

export function ModelsPage() {
  const meQ = useMgMe();
  const manage = !!meQ.data?.can_manage;

  if (meQ.error) {
    return (
      <div className={pageStyles.body}>
        <Alert
          type="error"
          showIcon
          message="连不上模型管理平台"
          description={`模型的管理端在模型管理平台上（atlas-modelgateway）：${meQ.error.message}`}
        />
      </div>
    );
  }

  return (
    <div className={pageStyles.page}>
      <header className={pageStyles.head}>
        <div className={pageStyles.titleRow}>
          <div>
            <h1 className={pageStyles.title}>模型</h1>
            <p className={pageStyles.desc}>
              模型调用直接走 LiteLLM，这里只管有哪些模型、谁能用、每把 Key 能用哪些。调用日志、花费、预算限流在 LiteLLM
              管理界面查看。
            </p>
          </div>
        </div>
      </header>
      <div className={pageStyles.body} style={{ paddingTop: 0 }}>
        <Tabs
          items={[
            { key: "models", label: "模型", children: <ModelsTab manage={manage} /> },
            { key: "keys", label: "API Key", children: <KeysTab manage={manage} litellmUrl={meQ.data?.litellm_url ?? null} /> },
          ]}
        />
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════ 模型列表

function ModelsTab({ manage }: { manage: boolean }) {
  const { modal, message } = AntdApp.useApp();
  const modelsQ = useMgModels();
  const providersQ = useMgProviders(true);
  const meQ = useMgMe();
  const remove = useDeleteMgModel();
  const [q, setQ] = useState("");
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
        (!vis || m.visibility === vis) &&
        (!source || m.source === source),
    );
  }, [models, q, vis, source]);
  const current = models.find((m) => rowKey(m) === detail);
  const grantModel = models.find((m) => m.uuid === granting);

  const confirmDelete = (m: MgModel) =>
    modal.confirm({
      title: `删除模型 ${m.name}`,
      okText: "删除",
      okButtonProps: { danger: true },
      content:
        m.status === "missing"
          ? "只删除平台记录和用户中心的数据对象（LiteLLM 中已经没有这个部署）。分配了它的 Key 会去掉它。"
          : m.managed
            ? `LiteLLM 中的部署、平台记录、用户中心的数据对象与授权一起删除，模型名 ${m.name} 立即不可调用。分配了它的 Key 会去掉它。`
            : "只删除平台记录和用户中心的数据对象；LiteLLM 里直接配置的部署保留（之后又显示为 LiteLLM 直配）。分配了它的 Key 会去掉它。",
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
    <>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message={
          manage
            ? "公有模型所有人可用，不需要授权；私有模型要授权给用户或部门。管理员拥有全部模型，给客户端 Key 分配模型时公有、私有都能选。LiteLLM 直配的模型「纳入平台」后才能放进 Key。"
            : `公有模型，以及授权给你或你所在部门的私有模型。调用时把「模型名」作为 model 参数，带上你的用户 Key 直接请求 LiteLLM${meQ.data?.litellm_url ? `（${meQ.data.litellm_url}）` : ""}。需要别的私有模型请联系管理员授权。`
        }
      />
      {modelsQ.data && !modelsQ.data.litellm_reachable && (
        <Alert type="warning" showIcon style={{ marginBottom: 12 }} message="LiteLLM 暂时连不上：模型是否可调用暂时未知，LiteLLM 直配的模型也列不出来" />
      )}
      {modelsQ.error && <Alert type="error" showIcon style={{ marginBottom: 12 }} message={modelsQ.error.message} />}
      <div className={pageStyles.filterRow}>
        <Input
          allowClear
          placeholder="搜索模型名 / 显示名称"
          style={{ width: 240 }}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        {manage && (
          <>
            <Select
              allowClear
              placeholder="全部可见性"
              style={{ width: 130 }}
              value={vis}
              onChange={setVis}
              options={[
                { value: "public", label: "公有" },
                { value: "private", label: "私有" },
              ]}
            />
            <Select
              allowClear
              placeholder="全部来源"
              style={{ width: 140 }}
              value={source}
              onChange={setSource}
              options={[
                { value: "platform", label: "平台登记" },
                { value: "litellm", label: "LiteLLM 直配" },
              ]}
            />
          </>
        )}
        <span className={styles.muted} style={{ marginLeft: "auto" }}>
          价格：美元 / 百万 token（输入 / 输出），由 LiteLLM 计费
        </span>
        {manage && (
          <Button type="primary" icon={<Icon name="plus" size={14} />} onClick={() => setForm({ kind: "new" })}>
            登记模型
          </Button>
        )}
      </div>
      <Table<MgModel>
        size="middle"
        rowKey={rowKey}
        loading={modelsQ.isPending}
        dataSource={rows}
        pagination={false}
        locale={{
          emptyText: (
            <Empty description={manage || q ? "没有符合条件的模型" : "还没有你能用的模型"} image={Empty.PRESENTED_IMAGE_SIMPLE} />
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
          { title: "供应商", width: 140, render: (_, m) => providerName(providers, m.provider) },
          {
            title: "上下文 / 最大输出",
            width: 150,
            render: (_, m) => `${fmtTokens(m.max_input_tokens)} / ${fmtTokens(m.max_output_tokens)}`,
          },
          { title: "价格", width: 120, render: (_, m) => fmtPrice(m) },
          ...(manage
            ? [
                { title: "可见性 / 授权", width: 240, render: (_: unknown, m: MgModel) => <AccessCell model={m} /> },
                { title: "来源", width: 120, render: (_: unknown, m: MgModel) => <SourceTag model={m} /> },
              ]
            : [
                {
                  title: "权限来源",
                  width: 160,
                  render: (_: unknown, m: MgModel) => (m.access === "public" ? "公有" : "授权给你或你所在的部门"),
                },
              ]),
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

      <Drawer
        open={!!current}
        width={560}
        onClose={() => setDetail(undefined)}
        title={current ? <DrawerTitle model={current} manage={manage} /> : null}
        extra={current && manage ? ops(current) : null}
      >
        {current && (
          <ModelDetail model={current} manage={manage} providers={providers} litellmUrl={meQ.data?.litellm_url ?? null} />
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
    </>
  );
}

const rowKey = (m: MgModel) => m.uuid ?? `litellm:${m.name}`;

function providerName(providers: MgProvider[], code: string): string {
  return providers.find((p) => p.code === code)?.name ?? (code || "—");
}

function SourceTag({ model: m }: { model: MgModel }) {
  if (m.source === "platform") return <Tag color="purple">平台登记</Tag>;
  return (
    <Tag title={`在 ${m.origin === "config" ? "LiteLLM 配置文件" : "LiteLLM 管理界面"} 中直接配置`}>LiteLLM 直配</Tag>
  );
}

function StatusTag({ model: m }: { model: MgModel }) {
  if (m.status === "missing")
    return (
      <Tag color="error" title="平台有记录，但 LiteLLM 中找不到对应部署">
        LiteLLM 中缺失
      </Tag>
    );
  if (m.status === "unknown") return <Tag>状态未知</Tag>;
  return <Tag color="success">可调用</Tag>;
}

function VisTag({ model: m }: { model: MgModel }) {
  return m.visibility === "public" ? <Tag color="blue">公有</Tag> : <Tag>私有</Tag>;
}

function AccessCell({ model: m }: { model: MgModel }) {
  if (m.source === "litellm") return <span className={styles.muted}>纳入平台后才能使用</span>;
  if (m.visibility === "public")
    return (
      <span>
        <VisTag model={m} />
        <span className={styles.muted}>所有人可用</span>
      </span>
    );
  const grants = m.grants ?? [];
  if (!grants.length)
    return (
      <span>
        <VisTag model={m} />
        <Tag color="warning" title="除管理员外没人能用">
          未授权
        </Tag>
      </span>
    );
  const labels = grants.map(grantLabel);
  return (
    <span>
      <VisTag model={m} />
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
  const origin =
    m.source === "platform"
      ? m.managed
        ? "平台登记（经 LiteLLM 接口加入）"
        : "LiteLLM 里直接配置，已纳入平台"
      : m.origin === "config"
        ? "LiteLLM 配置文件"
        : "LiteLLM 管理界面";
  return (
    <>
      {m.status === "missing" && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="LiteLLM 中找不到这个模型的部署"
          description="可能在 LiteLLM 界面里被删除了：现在调用会失败，也不会进入任何 Key。管理员可以重新登记（要重新填写供应商 API Key），或删除这条记录。"
        />
      )}
      {m.source === "platform" ? (
        <>
          <div className={styles.secTitle}>平台信息</div>
          <Kv k="显示名称" v={m.display_name} />
          <Kv k="供应商" v={providerName(providers, m.provider)} />
          <Kv k="可见性" v={m.visibility === "public" ? "公有：所有人可用，不需要授权" : "私有：授权给用户或部门后才能用"} />
          <Kv k="描述" v={m.description || "—"} />
          {m.created_at && <Kv k="登记时间" v={m.created_at.slice(0, 16).replace("T", " ")} />}
        </>
      ) : (
        <Alert
          type="info"
          showIcon
          message="这个模型在 LiteLLM 里直接配置，平台没有登记信息，下面只展示 LiteLLM 返回的字段。纳入平台后才能放进 Key。"
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
            <p>你的权限来源：授权给你或你所在的部门</p>
          )}
        </>
      )}
      {m.status !== "missing" && m.status !== "unknown" && (
        <>
          <div className={styles.secTitle}>LiteLLM 登记信息</div>
          <Kv k="来源" v={origin} />
          {m.deployments > 1 && <Kv k="部署数" v={`${m.deployments}（LiteLLM 在它们之间负载均衡）`} />}
          {manage && <Kv k="API Base" v={m.api_base || "供应商默认地址"} />}
          {manage && <Kv k="供应商 API Key" v="保存在 LiteLLM（加密），平台不保存" />}
          <Kv k="上下文窗口" v={fmtTokens(m.max_input_tokens)} />
          <Kv k="最大输出" v={fmtTokens(m.max_output_tokens)} />
          <Kv k="价格" v={`${fmtPrice(m)}（每百万 token，输入 / 输出）`} />
        </>
      )}
      <div className={styles.secTitle}>调用示例</div>
      <pre className={styles.code}>{curlFor(litellmUrl, m.name)}</pre>
      <p className={styles.muted} style={{ marginTop: 8 }}>
        把 $MODEL_API_KEY 换成你的用户 Key。嵌入、重排模型分别调 /v1/embeddings、/v1/rerank。
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
