"use client";

/**
 * 账号（管理员）：公司在各供应商的账号。模型的线路指向账号，同一个账号可以给很多模型用。
 *
 *   状态     正常 / 冷却中（限流、过载后歇一会，自动恢复）/ 异常（密钥失效、欠费：要人处理，
 *            测试连接通过后解除）/ 已停用
 *   余额     能查余额的供应商定时查；低于提醒线标出来
 *   测试连接 连通 → 列模型 → 发 1 个 token → 查余额，逐步给结果
 *
 * ★ 密钥加密保存，只显示末四位；修改时留空 = 不改。
 */
import { Alert, App as AntdApp, Button, Empty, Form, Input, InputNumber, Modal, Radio, Select, Space, Table, Tag } from "antd";
import { useState } from "react";

import {
  MG_HEALTH,
  type MgAccount,
  type MgAccountIn,
  type MgProbe,
  type MgProvider,
  useMgAccountActions,
  useMgAccounts,
  useMgProviders,
} from "@/api/model-gateway";
import { Icon } from "@/components/Icon";
import pageStyles from "@/features/agents/agents.module.css";
import styles from "@/features/catalog/catalog.module.css";
import { MgGate } from "./ModelsPage";
import { fmtTime, money } from "./shared";

const FORMAT: Record<string, string> = { anthropic: "Anthropic 格式", openai: "OpenAI 格式", google: "Gemini 原生" };
const ISSUE: Record<string, string> = { auth_failed: "密钥失效", no_balance: "余额不足" };

export function AccountsPage() {
  return (
    <MgGate>
      {(me) =>
        me.can_manage ? (
          <Accounts />
        ) : (
          <div className={pageStyles.body}>
            <Empty description="只有模型管理员能管理账号" />
          </div>
        )
      }
    </MgGate>
  );
}

function Accounts() {
  const { modal, message } = AntdApp.useApp();
  const accountsQ = useMgAccounts();
  const providersQ = useMgProviders();
  const { toggle, remove, test } = useMgAccountActions();
  const [form, setForm] = useState<{ account?: MgAccount }>();
  const [probe, setProbe] = useState<{ account: MgAccount; result?: MgProbe; error?: string }>();
  const accounts = accountsQ.data ?? [];
  const providers = providersQ.data ?? [];
  const bad = accounts.filter((a) => a.enabled && (a.status === "abnormal" || a.low_balance));

  const runTest = (a: MgAccount) => {
    setProbe({ account: a });
    test.mutate(
      { id: a.id },
      { onSuccess: (result) => setProbe({ account: a, result }), onError: (e) => setProbe({ account: a, error: e.message }) },
    );
  };
  const confirmDelete = (a: MgAccount) =>
    a.routes
      ? modal.info({ title: `${a.name} 还有 ${a.routes} 条线路在用`, content: "先在模型里去掉指向它的线路，或者改成停用。" })
      : modal.confirm({
          title: `删除账号 ${a.name}`,
          okText: "删除",
          okButtonProps: { danger: true },
          content: "删除后密钥一起删掉，不可恢复。",
          onOk: () => remove.mutateAsync(a.id).then(() => message.success("已删除"), (e: Error) => message.error(e.message)),
        });
  const setEnabled = (a: MgAccount, enabled: boolean) =>
    enabled
      ? toggle.mutate({ id: a.id, enabled }, { onSuccess: () => message.success("已启用"), onError: (e) => message.error(e.message) })
      : modal.confirm({
          title: `停用账号 ${a.name}`,
          okText: "停用",
          content: `${a.routes} 条线路不再用它；只有这一条线路的模型会调用失败。`,
          onOk: () =>
            toggle.mutateAsync({ id: a.id, enabled }).then(() => message.success("已停用"), (e: Error) => message.error(e.message)),
        });

  return (
    <div className={pageStyles.page}>
      <header className={pageStyles.head}>
        <div className={pageStyles.titleRow}>
          <h1 className={pageStyles.title}>账号</h1>
          <Button type="primary" className={pageStyles.action} icon={<Icon name="plus" size={14} />} onClick={() => setForm({})}>
            登记账号
          </Button>
        </div>
      </header>
      <div className={pageStyles.body}>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="公司在各供应商的账号：地址和密钥只在这里填一次，模型的线路指向账号。密钥加密保存，网关调用时才解开。"
        />
        {bad.length > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={bad
              .map((a) => `${a.name}：${a.status === "abnormal" ? `${ISSUE[a.issue?.kind ?? ""] ?? "异常"}，处理后点「测试连接」解除` : `余额 ${money(a.balance)}，低于提醒线`}`)
              .join("；")}
          />
        )}
        {accountsQ.error && <Alert type="error" showIcon style={{ marginBottom: 12 }} message={accountsQ.error.message} />}
        <Table<MgAccount>
          size="middle"
          rowKey="id"
          loading={accountsQ.isPending}
          dataSource={accounts}
          pagination={false}
          locale={{ emptyText: <Empty description="还没有账号" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
          columns={[
            {
              title: "账号",
              render: (_, a) => (
                <div>
                  <b>{a.name}</b> <Tag>{a.provider_name}</Tag>
                  <div className={styles.desc2}>
                    {Object.keys(a.endpoints).map((f) => FORMAT[f] ?? f).join(" · ")}
                    {a.egress === "proxy" && " · 经出网代理"}
                    {a.secret_masked && <span style={{ fontFamily: "var(--mono)" }}> · {a.secret_masked}</span>}
                  </div>
                </div>
              ),
            },
            { title: "线路", width: 70, render: (_, a) => a.routes },
            { title: "状态", width: 200, render: (_, a) => <HealthCell account={a} /> },
            {
              title: "余额",
              width: 130,
              render: (_, a) =>
                a.balance == null ? (
                  <span className={styles.muted}>—</span>
                ) : (
                  <div>
                    <span style={{ color: a.low_balance ? "var(--danger)" : undefined }}>{money(a.balance)}</span>
                    <div className={styles.desc2}>{fmtTime(a.balance_at)}</div>
                  </div>
                ),
            },
            {
              title: "最近检查",
              width: 170,
              render: (_, a) => (
                <div>
                  {fmtTime(a.checked_at)}
                  {a.check_error && (
                    <div className={styles.desc2} style={{ color: "var(--danger)" }} title={a.check_error}>
                      {a.check_error.slice(0, 40)}
                    </div>
                  )}
                </div>
              ),
            },
            {
              title: "操作",
              width: 250,
              render: (_, a) => (
                <Space size={0}>
                  <Button size="small" type="link" loading={test.isPending && test.variables?.id === a.id} onClick={() => runTest(a)}>
                    测试连接
                  </Button>
                  <Button size="small" type="link" onClick={() => setForm({ account: a })}>
                    编辑
                  </Button>
                  <Button size="small" type="link" onClick={() => setEnabled(a, !a.enabled)}>
                    {a.enabled ? "停用" : "启用"}
                  </Button>
                  <Button size="small" type="link" danger onClick={() => confirmDelete(a)}>
                    删除
                  </Button>
                </Space>
              ),
            },
          ]}
        />
      </div>
      {form && <AccountFormModal account={form.account} providers={providers} onClose={() => setForm(undefined)} />}
      {probe && (
        <Modal open title={`测试连接 · ${probe.account.name}`} footer={null} onCancel={() => setProbe(undefined)}>
          {probe.error ? (
            <Alert type="error" showIcon message={probe.error} />
          ) : probe.result ? (
            <ProbeResult result={probe.result} />
          ) : (
            <p className={styles.muted}>测试中…</p>
          )}
        </Modal>
      )}
    </div>
  );
}

function HealthCell({ account: a }: { account: MgAccount }) {
  const h = MG_HEALTH[a.status];
  return (
    <div>
      <Tag color={h.color}>{h.label}</Tag>
      {a.status === "abnormal" && a.issue && (
        <div className={styles.desc2} title={a.issue.message}>
          {ISSUE[a.issue.kind] ?? a.issue.kind} · {fmtTime(a.issue.since)} 起
        </div>
      )}
      {a.status === "cooling" && <div className={styles.desc2}>{a.cooling_routes} 条线路冷却中，会自动恢复</div>}
    </div>
  );
}

function ProbeResult({ result: r }: { result: MgProbe }) {
  return (
    <>
      <Alert
        type={r.ok ? "success" : "error"}
        showIcon
        style={{ marginBottom: 12 }}
        message={r.ok ? "连接正常" : "有步骤没通过"}
      />
      {r.steps.map((s) => (
        <div key={s.name} className={styles.kv}>
          <span>
            <span style={{ color: s.ok ? "var(--success)" : "var(--danger)" }}>{s.ok ? "✓" : "✗"}</span> {s.name}
          </span>
          <span>{s.message || "—"}</span>
        </div>
      ))}
      {r.upstream_models && r.upstream_models.length > 0 && (
        <>
          <div className={styles.secTitle}>供应商那边的模型（{r.upstream_models.length}）</div>
          <div style={{ maxHeight: 160, overflowY: "auto" }}>
            {r.upstream_models.map((m) => (
              <Tag key={m} style={{ fontFamily: "var(--mono)", marginBottom: 4 }}>
                {m}
              </Tag>
            ))}
          </div>
        </>
      )}
    </>
  );
}

interface AccountValues {
  name: string;
  provider: string;
  endpoints: Record<string, string>;
  secret?: string;
  egress: "direct" | "proxy";
  first_content_timeout_s?: number | null;
  balance_alert?: number | null;
  remark?: string;
  upstream_model?: string;
}

function AccountFormModal({ account: a, providers, onClose }: { account?: MgAccount; providers: MgProvider[]; onClose: () => void }) {
  const { message } = AntdApp.useApp();
  const [form] = Form.useForm<AccountValues>();
  const { save, test, testDraft } = useMgAccountActions();
  const [probe, setProbe] = useState<{ result?: MgProbe; error?: string }>();
  const providerCode = Form.useWatch("provider", form) ?? a?.provider;
  const provider = providers.find((p) => p.code === providerCode);
  const isNew = !a;

  const pickProvider = (code: string) => {
    const p = providers.find((x) => x.code === code);
    if (!p) return;
    form.setFieldValue("endpoints", { ...p.endpoints });
    form.setFieldValue("egress", p.overseas ? "proxy" : "direct");
    if (!form.getFieldValue("name")) form.setFieldValue("name", p.name);
    setProbe(undefined);
  };

  const body = (v: AccountValues): MgAccountIn => ({
    name: v.name.trim(),
    provider: v.provider,
    endpoints: Object.fromEntries(Object.entries(v.endpoints ?? {}).filter(([, url]) => url?.trim())),
    secret: v.secret?.trim() || null,
    egress: v.egress,
    first_content_timeout_s: v.first_content_timeout_s ?? null,
    balance_alert: v.balance_alert ?? null,
    remark: v.remark?.trim() ?? "",
    enabled: a?.enabled ?? true,
  });

  /** 编辑时密钥留空：测已保存的配置（密钥只在服务端）；填了新密钥就按填的测 */
  const runTest = async () => {
    const v = await form.validateFields();
    const upstream = v.upstream_model?.trim() || undefined;
    const done = { onSuccess: (result: MgProbe) => setProbe({ result }), onError: (e: Error) => setProbe({ error: e.message }) };
    setProbe({});
    if (a && !v.secret?.trim()) test.mutate({ id: a.id, upstream_model: upstream }, done);
    else testDraft.mutate({ ...body(v), upstream_model: upstream }, done);
  };

  const submit = async () => {
    const v = await form.validateFields();
    save.mutate(
      { id: a?.id, body: body(v) },
      {
        onSuccess: () => {
          message.success(isNew ? `已登记 ${v.name.trim()}` : "已保存");
          onClose();
        },
        onError: (e) => message.error(e.message),
      },
    );
  };

  return (
    <Modal
      open
      width={680}
      title={isNew ? "登记账号" : `编辑账号 · ${a.name}`}
      okText={isNew ? "登记" : "保存"}
      okButtonProps={{ loading: save.isPending }}
      onCancel={onClose}
      onOk={submit}
      destroyOnHidden
    >
      <Form
        form={form}
        layout="vertical"
        requiredMark
        initialValues={
          a
            ? {
                name: a.name,
                provider: a.provider,
                endpoints: a.endpoints,
                egress: a.egress,
                first_content_timeout_s: a.first_content_timeout_s,
                balance_alert: a.balance_alert == null ? null : Number(a.balance_alert),
                remark: a.remark,
              }
            : { egress: "direct", endpoints: {} }
        }
      >
        <Space.Compact block style={{ gap: 12 }}>
          <Form.Item name="provider" label="供应商" style={{ flex: 1 }} rules={[{ required: true, message: "选供应商" }]}>
            <Select
              disabled={!isNew}
              showSearch
              optionFilterProp="label"
              placeholder="选供应商"
              onChange={pickProvider}
              options={providers.map((p) => ({ value: p.code, label: p.name }))}
            />
          </Form.Item>
          <Form.Item name="name" label="账号名" style={{ flex: 1 }} rules={[{ required: true, whitespace: true, message: "填账号名" }]}>
            <Input maxLength={100} placeholder="如 DeepSeek 公司账号" />
          </Form.Item>
        </Space.Compact>
        {provider?.hint && <p className={styles.muted} style={{ marginTop: -8, marginBottom: 12 }}>{provider.hint}</p>}
        {provider && (
          <>
            {provider.formats.map((f, i) => (
              <Form.Item
                key={f}
                name={["endpoints", f]}
                label={`${FORMAT[f] ?? f} 地址${i === 0 && provider.formats.length > 1 ? "（推荐）" : ""}`}
                extra={i === 0 && provider.formats.length > 1 ? "不用的格式清空即可；网关优先用和调用方同格式的地址，省一次转换" : undefined}
                rules={[{ required: provider.needs_endpoint && i === 0, message: "这个供应商要填地址" }]}
              >
                <Input placeholder={provider.endpoints[f] || "如 http://10.0.0.8:8000/v1"} />
              </Form.Item>
            ))}
            <Form.Item
              name="secret"
              label="密钥"
              extra="加密保存，保存后只显示末四位"
              rules={[{ required: isNew && !provider.key_optional, message: "填供应商控制台申请的 API Key" }]}
            >
              <Input.Password autoComplete="new-password" placeholder={isNew ? "供应商控制台申请的 API Key" : `${a?.secret_masked ?? ""}（留空 = 不改）`} />
            </Form.Item>
          </>
        )}
        <Form.Item
          name="egress"
          label="出网方式"
          extra={provider?.overseas ? "境外供应商一般要经出网代理（代理地址由运维配置）" : undefined}
        >
          <Radio.Group>
            <Radio value="direct">直连</Radio>
            <Radio value="proxy">经出网代理</Radio>
          </Radio.Group>
        </Form.Item>
        <Space.Compact block style={{ gap: 12 }}>
          <Form.Item name="first_content_timeout_s" label="等第一个字最多（秒）" style={{ flex: 1 }} extra="不填用默认 600 秒；推理慢的模型别设太短">
            <InputNumber min={10} max={3600} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item
            name="balance_alert"
            label="余额提醒线（元）"
            style={{ flex: 1 }}
            extra={provider && !provider.can_balance ? "这个供应商查不了余额" : "低于它在列表里标红"}
          >
            <InputNumber min={0} disabled={!!provider && !provider.can_balance} style={{ width: "100%" }} />
          </Form.Item>
        </Space.Compact>
        <Form.Item name="remark" label="备注">
          <Input.TextArea rows={2} maxLength={1000} placeholder="充值方式、联系人、额度说明" />
        </Form.Item>
        <Space align="start">
          <Form.Item name="upstream_model" style={{ marginBottom: 8 }}>
            <Input style={{ width: 240 }} placeholder="试用的上游模型（可不填）" />
          </Form.Item>
          <Button loading={testDraft.isPending || test.isPending} onClick={() => void runTest()}>
            测试连接
          </Button>
        </Space>
      </Form>
      {probe && (probe.error ? <Alert type="error" showIcon message={probe.error} /> : probe.result ? <ProbeResult result={probe.result} /> : null)}
    </Modal>
  );
}
