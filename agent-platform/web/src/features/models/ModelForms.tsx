"use client";

/**
 * 模型的登记、编辑、重新登记、纳入平台，以及私有模型的授权。
 *
 *   登记       平台基本信息 + 只交给 LiteLLM 的路由（地址、供应商凭据、规格价格），私有的可一并授权
 *   编辑       模型名不能改；供应商凭据留空 = 不改。纳入平台的（LiteLLM 直配）只改平台信息
 *   重新登记   LiteLLM 里的部署没了：按新填的路由重建，可见性和授权不变
 *   纳入平台   LiteLLM 直配的模型：只写平台记录、在用户中心登记，不改 LiteLLM
 *
 * ★ 授权只给人和部门（可含下级）：智能体用客户端 Key，由管理员直接分配模型，不看授权。
 */
import { useQuery } from "@tanstack/react-query";
import { Alert, App as AntdApp, Button, Checkbox, Form, Input, InputNumber, Modal, Radio, Select, Space, Table, Tag, TreeSelect } from "antd";
import { useMemo, useState } from "react";

import {
  type MgGrantIn,
  type MgModel,
  type MgProvider,
  type MgVisibility,
  useMgGrantActions,
  useSaveMgModel,
  useTestMgConnection,
} from "@/api/model-gateway";
import styles from "@/features/catalog/catalog.module.css";
import { type DirDept, directory } from "@/features/knowledge/api";
import { fmtPrice, fmtTokens, grantLabel } from "./shared";

export type FormKind = "new" | "edit" | "reregister" | "adopt";

const TITLE: Record<FormKind, string> = { new: "登记模型", edit: "编辑模型", reregister: "重新登记模型", adopt: "纳入平台" };
const OK_TEXT: Record<FormKind, string> = { new: "登记", edit: "保存", reregister: "重新登记", adopt: "纳入" };
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._:/-]*$/;

interface FormValues {
  name: string;
  display_name: string;
  provider: string;
  visibility: MgVisibility;
  description: string;
  api_base?: string;
  api_key?: string;
  max_input_tokens?: number | null;
  max_output_tokens?: number | null;
  input_price?: number | null;
  output_price?: number | null;
}

type DraftGrant = MgGrantIn & { label: string };

export function ModelFormModal({
  kind,
  model: m,
  models,
  providers,
  onClose,
}: {
  kind: FormKind;
  model?: MgModel;
  models: MgModel[];
  providers: MgProvider[];
  onClose: () => void;
}) {
  const { message } = AntdApp.useApp();
  const [form] = Form.useForm<FormValues>();
  const save = useSaveMgModel();
  const test = useTestMgConnection();
  const [grants, setGrants] = useState<DraftGrant[]>([]);
  const [tested, setTested] = useState<{ ok: boolean; message: string }>();
  // 纳入平台的（不是平台在 LiteLLM 里建的部署）编辑时不动 LiteLLM
  const hasRoute = kind === "new" || kind === "reregister" || (kind === "edit" && !!m?.managed);
  const hasGrants = kind === "new" || kind === "adopt";
  const visibility = Form.useWatch("visibility", form);
  const providerCode = Form.useWatch("provider", form);
  const provider = providers.find((p) => p.code === providerCode);
  const keyRequired = (kind === "new" || kind === "reregister") && !provider?.key_optional;

  const initial: Partial<FormValues> = {
    name: m?.name ?? "",
    display_name: m?.display_name ?? "",
    provider: m?.provider || providers[0]?.code,
    visibility: m?.visibility ?? "private",
    description: m?.description ?? "",
    api_base: m?.api_base ?? "",
    max_input_tokens: m?.max_input_tokens,
    max_output_tokens: m?.max_output_tokens,
    input_price: m?.input_price,
    output_price: m?.output_price,
  };

  const route = (v: FormValues) => ({
    api_base: v.api_base?.trim() || null,
    api_key: v.api_key?.trim() || null,
    max_input_tokens: v.max_input_tokens ?? null,
    max_output_tokens: v.max_output_tokens ?? null,
    input_price: v.input_price ?? null,
    output_price: v.output_price ?? null,
  });

  const runTest = async () => {
    const v = form.getFieldsValue(true) as FormValues;
    if (!v.name?.trim() || (!provider?.key_optional && !v.api_key?.trim())) {
      setTested({ ok: false, message: "先填写模型名和供应商 API Key" });
      return;
    }
    setTested(undefined);
    test.mutate(
      { name: v.name.trim(), provider: v.provider, ...route(v) },
      { onSuccess: setTested, onError: (e) => setTested({ ok: false, message: e.message }) },
    );
  };

  const submit = async () => {
    const v = await form.validateFields();
    const base = {
      display_name: v.display_name.trim(),
      provider: v.provider,
      visibility: v.visibility,
      description: v.description?.trim() ?? "",
    };
    const picked = v.visibility === "private" ? grants.map(({ label: _, ...g }) => g) : [];
    const input =
      kind === "new"
        ? { kind, body: { ...base, ...route(v), name: v.name.trim(), grants: picked } }
        : kind === "edit"
          ? { kind, id: m!.uuid!, body: { ...base, ...(hasRoute ? route(v) : {}) } }
          : kind === "reregister"
            ? { kind, id: m!.uuid!, body: { provider: v.provider, ...route(v) } }
            : {
                kind,
                body: {
                  name: m!.name,
                  display_name: base.display_name,
                  visibility: base.visibility,
                  description: base.description,
                  grants: picked,
                },
              };
    save.mutate(input as Parameters<typeof save.mutate>[0], {
      onSuccess: () => {
        message.success(`${OK_TEXT[kind] === "保存" ? "已保存" : `已${OK_TEXT[kind]}`} ${m?.name ?? v.name}`);
        onClose();
      },
      onError: (e) => message.error(e.message),
    });
  };

  return (
    <Modal
      open
      width={680}
      title={TITLE[kind]}
      okText={OK_TEXT[kind]}
      okButtonProps={{ loading: save.isPending }}
      onCancel={onClose}
      onOk={submit}
      destroyOnHidden
    >
      {kind === "reregister" && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="LiteLLM 中的部署已不存在。重新登记会按下面的参数在 LiteLLM 新建部署，并把平台记录指向它；可见性和授权不变。供应商 API Key 平台没有保存，需要重新填写。"
        />
      )}
      {kind === "adopt" && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message={`这个模型已在 LiteLLM 里配置（${m?.origin === "config" ? "配置文件" : "管理界面"}）。纳入平台只写平台记录、在用户中心登记，不改 LiteLLM；纳入后才能放进 Key。`}
        />
      )}
      <Form form={form} layout="vertical" initialValues={initial} requiredMark>
        {kind !== "reregister" && (
          <>
            <div className={styles.secTitle}>基本信息（平台保存）</div>
            <Space.Compact block style={{ gap: 12 }}>
              <Form.Item
                name="display_name"
                label="显示名称"
                style={{ flex: 1 }}
                rules={[{ required: true, whitespace: true, message: "请填写显示名称" }]}
              >
                <Input placeholder="如 DeepSeek V3.2" maxLength={100} />
              </Form.Item>
              <Form.Item name="provider" label="供应商" style={{ flex: 1 }} rules={[{ required: true }]}>
                <Select
                  disabled={kind === "adopt" || (kind === "edit" && !m?.managed)}
                  options={providers.map((p) => ({ value: p.code, label: p.name }))}
                />
              </Form.Item>
            </Space.Compact>
            <Form.Item
              name="visibility"
              label="可见性"
              extra={
                kind === "edit" && visibility !== m?.visibility
                  ? visibility === "public"
                    ? "改成公有后所有人都能用，已有的授权保留但不再起作用。"
                    : "改成私有后，没被授权的人的用户 Key 里的这个模型会失效；客户端 Key 不受影响。"
                  : undefined
              }
            >
              <Radio.Group>
                <Radio value="private">
                  私有 <span className={styles.muted}>（授权给用户或部门后才能用）</span>
                </Radio>
                <Radio value="public">
                  公有 <span className={styles.muted}>（所有人可用，不需要授权）</span>
                </Radio>
              </Radio.Group>
            </Form.Item>
            <Form.Item name="description" label="描述">
              <Input.TextArea rows={2} maxLength={2000} placeholder="用途、特点，给使用的人看" />
            </Form.Item>
          </>
        )}
        {kind === "reregister" && (
          <Form.Item name="provider" label="供应商" rules={[{ required: true }]}>
            <Select options={providers.map((p) => ({ value: p.code, label: p.name }))} />
          </Form.Item>
        )}

        {hasRoute ? (
          <>
            <div className={styles.secTitle}>路由（只提交给 LiteLLM）</div>
            <Space.Compact block style={{ gap: 12 }}>
              <Form.Item
                name="name"
                label="模型名"
                style={{ flex: 1 }}
                extra="供应商侧的模型名，调用时传的 model 就是它；登记后不能修改"
                rules={[
                  { required: true, whitespace: true, message: "请填写模型名" },
                  { pattern: NAME_RE, message: "只能包含字母、数字和 . _ : / -" },
                  {
                    validator: (_, value: string) =>
                      kind === "new" && models.some((x) => x.name === value?.trim())
                        ? Promise.reject(new Error("已有同名模型（含 LiteLLM 直配的）；模型名就是调用名，不能重复"))
                        : Promise.resolve(),
                  },
                ]}
              >
                <Input disabled={kind !== "new"} placeholder="如 deepseek-chat" />
              </Form.Item>
              <Form.Item
                name="api_base"
                label="API Base"
                style={{ flex: 1 }}
                rules={[{ required: !!provider?.needs_base, message: `${provider?.name ?? ""} 需要填写 API Base` }]}
              >
                <Input placeholder={provider?.needs_base ? "如 http://10.0.0.8:8000/v1" : "留空用供应商默认地址"} />
              </Form.Item>
            </Space.Compact>
            <Form.Item
              name="api_key"
              label="供应商 API Key"
              extra="只提交给 LiteLLM 加密保存，平台不落库"
              rules={[{ required: keyRequired, message: "请填写供应商 API Key" }]}
            >
              <Input.Password
                autoComplete="new-password"
                placeholder={kind === "edit" ? "留空表示不修改" : "供应商控制台申请的 Key"}
              />
            </Form.Item>
            <Space style={{ marginBottom: 12 }}>
              <Button size="small" loading={test.isPending} onClick={runTest}>
                测试连接
              </Button>
              {tested && (
                <span style={{ color: tested.ok ? "var(--success)" : "var(--danger)" }}>
                  {tested.ok ? "✓ 连接成功" : `✗ ${tested.message}`}
                </span>
              )}
            </Space>
            <div className={styles.secTitle}>规格与价格（写入 LiteLLM）</div>
            <Space.Compact block style={{ gap: 12 }}>
              <Form.Item name="max_input_tokens" label="上下文窗口（token）" style={{ flex: 1 }}>
                <InputNumber min={1} style={{ width: "100%" }} placeholder="如 131072" />
              </Form.Item>
              <Form.Item name="max_output_tokens" label="最大输出（token）" style={{ flex: 1 }}>
                <InputNumber min={1} style={{ width: "100%" }} placeholder="如 8192" />
              </Form.Item>
            </Space.Compact>
            <Space.Compact block style={{ gap: 12 }}>
              <Form.Item name="input_price" label="输入价格（美元 / 百万 token）" style={{ flex: 1 }}>
                <InputNumber min={0} step={0.01} style={{ width: "100%" }} placeholder="自部署可留空" />
              </Form.Item>
              <Form.Item name="output_price" label="输出价格（美元 / 百万 token）" style={{ flex: 1 }}>
                <InputNumber min={0} step={0.01} style={{ width: "100%" }} placeholder="自部署可留空" />
              </Form.Item>
            </Space.Compact>
          </>
        ) : (
          m && (
            <>
              <div className={styles.secTitle}>LiteLLM 中的配置（只读，到 LiteLLM 管理界面修改）</div>
              <Kv k="模型名" v={m.name} />
              <Kv k="API Base" v={m.api_base || "供应商默认地址"} />
              <Kv k="上下文 / 输出" v={`${fmtTokens(m.max_input_tokens)} / ${fmtTokens(m.max_output_tokens)}`} />
              <Kv k="价格" v={fmtPrice(m)} />
            </>
          )
        )}
      </Form>

      {hasGrants && visibility === "private" && (
        <>
          <div className={styles.secTitle}>授权（私有模型，登记后也可以改）</div>
          <div style={{ marginBottom: 8 }}>
            {grants.length ? (
              grants.map((g) => (
                <Tag
                  key={`${g.subject_type}:${g.subject_id}`}
                  closable
                  onClose={() => setGrants((gs) => gs.filter((x) => x !== g))}
                >
                  {g.label}
                </Tag>
              ))
            ) : (
              <span className={styles.muted}>还没有授权：除管理员外没人能用这个模型</span>
            )}
          </div>
          <GrantPicker
            onAdd={(g) =>
              setGrants((gs) => [
                ...gs.filter((x) => !(x.subject_type === g.subject_type && x.subject_id === g.subject_id)),
                g,
              ])
            }
          />
        </>
      )}
    </Modal>
  );
}

export function GrantsModal({ model: m, onClose }: { model: MgModel; onClose: () => void }) {
  const { message } = AntdApp.useApp();
  const { add, revoke } = useMgGrantActions(m.uuid!);
  const grants = (m.grants ?? []).filter((g) => g.permission === "READ");

  return (
    <Modal open width={640} title={`授权 · ${m.display_name}`} footer={null} onCancel={onClose}>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="私有模型授权给用户或部门后，他们才能把它放进自己的用户 Key；撤销后 Key 里的这个模型随之失效。管理员拥有全部模型，不用在这里授权；客户端 Key 也不看授权，由管理员直接分配。"
      />
      <Table
        size="small"
        rowKey="id"
        dataSource={grants}
        pagination={false}
        locale={{ emptyText: "还没有授权：除管理员外没人能用这个模型" }}
        columns={[
          { title: "授权对象", render: (_, g) => grantLabel(g) },
          { title: "类型", width: 80, render: (_, g) => (g.subject_type === "dept" ? "部门" : "用户") },
          { title: "级别", width: 80, render: () => "使用" },
          {
            title: "",
            width: 70,
            render: (_, g) => (
              <Button
                size="small"
                type="link"
                danger
                loading={revoke.isPending && revoke.variables === g.id}
                onClick={() =>
                  revoke.mutate(g.id, {
                    onSuccess: () => message.success("已撤销"),
                    onError: (e) => message.error(e.message),
                  })
                }
              >
                撤销
              </Button>
            ),
          },
        ]}
      />
      <div style={{ marginTop: 12 }}>
        <GrantPicker
          loading={add.isPending}
          onAdd={({ label: _, ...g }) =>
            add.mutate([g], {
              onSuccess: () => message.success("已授权"),
              onError: (e) => message.error(e.message),
            })
          }
        />
      </div>
    </Modal>
  );
}

// ═══════════════════════════════════════════════ 选人、选部门

interface DeptNode {
  title: string;
  value: string;
  children: DeptNode[];
}

function deptTree(rows: DirDept[]): DeptNode[] {
  const byParent = new Map<string | null, DirDept[]>();
  for (const d of rows) byParent.set(d.parent_id, [...(byParent.get(d.parent_id) ?? []), d]);
  const build = (parent: string | null): DeptNode[] =>
    (byParent.get(parent) ?? []).map((d) => ({ title: d.name, value: d.id, children: build(d.id) }));
  return build(null);
}

function GrantPicker({ onAdd, loading }: { onAdd: (g: DraftGrant) => void; loading?: boolean }) {
  const [kind, setKind] = useState<"dept" | "user">("dept");
  const [q, setQ] = useState("");
  const [target, setTarget] = useState<string>();
  const [includeSub, setIncludeSub] = useState(true);
  const users = useQuery({
    queryKey: ["directory", "users", q],
    queryFn: () => directory.users(q),
    staleTime: 30_000,
    enabled: kind === "user",
  });
  const depts = useQuery({ queryKey: ["directory", "depts"], queryFn: directory.depts, staleTime: 60_000 });
  const tree = useMemo(() => deptTree(depts.data ?? []), [depts.data]);
  // 授权只给人：数字员工用的是客户端 Key
  const people = (users.data ?? []).filter((u) => u.user_type !== "digital");

  const add = () => {
    if (!target) return;
    if (kind === "dept") {
      const name = depts.data?.find((d) => d.id === target)?.name ?? target;
      onAdd({
        subject_type: "dept",
        subject_id: target,
        include_sub: includeSub,
        label: `${name}${includeSub ? "（含下级）" : ""}`,
      });
    } else {
      const u = people.find((x) => x.id === target);
      onAdd({ subject_type: "user", subject_id: target, include_sub: false, label: u?.name ?? target });
    }
    setTarget(undefined);
  };

  return (
    <Space wrap>
      <Radio.Group
        size="small"
        value={kind}
        onChange={(e) => {
          setKind(e.target.value);
          setTarget(undefined);
        }}
      >
        <Radio.Button value="dept">部门</Radio.Button>
        <Radio.Button value="user">用户</Radio.Button>
      </Radio.Group>
      {kind === "dept" ? (
        <>
          <TreeSelect
            style={{ width: 240 }}
            treeData={tree}
            value={target}
            onChange={setTarget}
            placeholder="选择部门"
            treeDefaultExpandAll
            showSearch
            treeNodeFilterProp="title"
          />
          <Checkbox checked={includeSub} onChange={(e) => setIncludeSub(e.target.checked)}>
            含下级部门
          </Checkbox>
        </>
      ) : (
        <Select
          style={{ width: 240 }}
          showSearch
          filterOption={false}
          onSearch={setQ}
          value={target}
          onChange={setTarget}
          placeholder="输入名字或账号搜索"
          options={people.map((u) => ({ value: u.id, label: `${u.name}（${u.account}）` }))}
        />
      )}
      <Button size="small" type="primary" disabled={!target} loading={loading} onClick={add}>
        添加
      </Button>
    </Space>
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
