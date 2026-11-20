"use client";

/**
 * 添加、编辑模型，以及私有模型的授权。
 *
 *   基本信息   模型名（调用时传的 model，添加后不能改）、显示名称、类型、谁能用、描述
 *   线路       哪个账号、上游叫什么（空 = 和模型名一样）、优先级（小的先用）、权重（同优先级分流）
 *   能力与价格 能看图、工具调用、思考、上下文、最大输出：按第一条线路的预置带出，可以改；
 *              价格是对内统一价（元 / 百万 token），不填 = 不计费
 *
 * ★ 授权只给人和部门（可含下级）：智能体用客户端 Key，由管理员直接分配模型，不看授权。
 */
import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  App as AntdApp,
  Button,
  Checkbox,
  Form,
  Input,
  InputNumber,
  Modal,
  Radio,
  Select,
  Space,
  Table,
  Tag,
  TreeSelect,
} from "antd";
import { useMemo, useState } from "react";

import {
  type MgCaps,
  type MgGrantIn,
  type MgKind,
  type MgModel,
  type MgVisibility,
  suggestCaps,
  useMgAccounts,
  useMgGrantActions,
  useMgModelActions,
} from "@/api/model-gateway";
import { Icon } from "@/components/Icon";
import styles from "@/features/catalog/catalog.module.css";
import { type DirDept, directory } from "@/features/knowledge/api";
import { grantLabel } from "./shared";

const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/;

interface RouteValues {
  account_id?: string;
  upstream_model?: string;
  priority?: number;
  weight?: number;
}

interface FormValues {
  name: string;
  display_name: string;
  kind: MgKind;
  visibility: MgVisibility;
  description: string;
  routes: RouteValues[];
  caps: MgCaps;
  price: { input?: number | null; cache_hit?: number | null; output?: number | null };
}

type DraftGrant = MgGrantIn & { label: string };

const EMPTY_CAPS: MgCaps = { vision: false, tools: true, thinking: "none", context_window: null, max_output: null };

export function ModelFormModal({ model: m, models, onClose }: { model?: MgModel; models: MgModel[]; onClose: () => void }) {
  const { message } = AntdApp.useApp();
  const [form] = Form.useForm<FormValues>();
  const { save } = useMgModelActions();
  const accountsQ = useMgAccounts();
  const [grants, setGrants] = useState<DraftGrant[]>([]);
  // 添加时能力没动过：按第一条线路的预置自动带出；动过就不再覆盖
  const [capsTouched, setCapsTouched] = useState(!!m);
  const [suggestNote, setSuggestNote] = useState<string>();
  const isNew = !m;
  const visibility = Form.useWatch("visibility", form);
  const kind = Form.useWatch("kind", form) ?? m?.kind ?? "chat";
  const accounts = accountsQ.data ?? [];

  const initial: Partial<FormValues> = {
    name: m?.name ?? "",
    display_name: m?.display_name ?? "",
    kind: m?.kind ?? "chat",
    visibility: m?.visibility ?? "private",
    description: m?.description ?? "",
    routes: m?.routes.length
      ? m.routes.map((r) => ({ account_id: r.account_id, upstream_model: r.upstream_model, priority: r.priority, weight: r.weight }))
      : [{ priority: 1, weight: 1 }],
    caps: m?.caps ?? EMPTY_CAPS,
    price: m
      ? { input: Number(m.price.input), cache_hit: Number(m.price.cache_hit), output: Number(m.price.output) }
      : { input: null, cache_hit: null, output: null },
  };

  /** 按第一条线路（账号 + 上游模型）带出能力 */
  const fillCaps = async (force: boolean) => {
    if (!force && capsTouched) return;
    const v = form.getFieldsValue(true) as FormValues;
    const first = v.routes?.[0];
    const upstream = first?.upstream_model?.trim() || v.name?.trim();
    if (!first?.account_id || !upstream) {
      if (force) setSuggestNote("先选第一条线路的账号，填模型名或上游模型");
      return;
    }
    try {
      const r = await suggestCaps(first.account_id, upstream, v.kind ?? "chat");
      form.setFieldValue("caps", r.caps);
      setSuggestNote(r.preset ? `已按 ${upstream} 的预置带出` : `${upstream} 没有预置，给的是保守值，请按供应商文档核对`);
    } catch (e) {
      setSuggestNote((e as Error).message);
    }
  };

  const submit = async () => {
    const v = await form.validateFields();
    const body = {
      display_name: v.display_name.trim(),
      visibility: v.visibility,
      description: v.description?.trim() ?? "",
      caps: v.caps,
      price: { input: v.price?.input ?? 0, cache_hit: v.price?.cache_hit ?? 0, output: v.price?.output ?? 0 },
      enabled: m?.enabled ?? true,
      routes: v.routes.map((r) => ({
        account_id: r.account_id!,
        upstream_model: r.upstream_model?.trim() ?? "",
        priority: r.priority ?? 1,
        weight: r.weight ?? 1,
      })),
    };
    const input = isNew
      ? {
          kind: "new" as const,
          body: {
            ...body,
            name: v.name.trim(),
            kind: v.kind,
            grants: v.visibility === "private" ? grants.map(({ label: _, ...g }) => g) : [],
          },
        }
      : { kind: "edit" as const, id: m.id, body };
    save.mutate(input, {
      onSuccess: () => {
        message.success(isNew ? `已添加 ${v.name.trim()}` : "已保存");
        onClose();
      },
      onError: (e) => message.error(e.message),
    });
  };

  return (
    <Modal
      open
      width={760}
      title={isNew ? "添加模型" : `编辑模型 · ${m.name}`}
      okText={isNew ? "添加" : "保存"}
      okButtonProps={{ loading: save.isPending }}
      onCancel={onClose}
      onOk={submit}
      destroyOnHidden
    >
      {!accountsQ.isPending && !accounts.length && (
        <Alert type="warning" showIcon style={{ marginBottom: 12 }} message="还没有账号：先在「账号」里登记供应商账号，模型的线路要指向账号。" />
      )}
      <Form
        form={form}
        layout="vertical"
        initialValues={initial}
        requiredMark
        onValuesChange={(changed: Partial<FormValues>) => {
          if (changed.caps) setCapsTouched(true);
        }}
      >
        <div className={styles.secTitle}>基本信息</div>
        <Space.Compact block style={{ gap: 12 }}>
          <Form.Item
            name="name"
            label="模型名"
            style={{ flex: 1 }}
            extra={isNew ? "调用时传的 model；添加后不能改" : undefined}
            rules={[
              { required: true, whitespace: true, message: "请填写模型名" },
              { pattern: NAME_RE, message: "只能包含字母、数字和 . _ : / -，最长 128 个字符" },
              {
                validator: (_, value: string) =>
                  isNew && models.some((x) => x.name === value?.trim())
                    ? Promise.reject(new Error("已有同名模型；模型名就是调用名，不能重复"))
                    : Promise.resolve(),
              },
            ]}
          >
            <Input disabled={!isNew} placeholder="如 deepseek-v4-pro" onBlur={() => void fillCaps(false)} />
          </Form.Item>
          <Form.Item name="display_name" label="显示名称" style={{ flex: 1 }}>
            <Input placeholder="不填用模型名" maxLength={100} />
          </Form.Item>
        </Space.Compact>
        <Space.Compact block style={{ gap: 12 }}>
          <Form.Item name="kind" label="类型" style={{ flex: 1 }}>
            <Radio.Group disabled={!isNew} onChange={() => void fillCaps(false)}>
              <Radio value="chat">对话</Radio>
              <Radio value="embedding">向量</Radio>
              <Radio value="rerank">重排</Radio>
            </Radio.Group>
          </Form.Item>
          <Form.Item
            name="visibility"
            label="谁能用"
            style={{ flex: 1 }}
            extra={
              !isNew && visibility !== m.visibility
                ? visibility === "public"
                  ? "改成公有后所有人都能用，已有的授权保留但不再起作用。"
                  : "改成私有后，没被授权的人的 Key 里它会失效；客户端 Key 不受影响。"
                : undefined
            }
          >
            <Radio.Group>
              <Radio value="private">私有（授权后才能用）</Radio>
              <Radio value="public">公有（所有人）</Radio>
            </Radio.Group>
          </Form.Item>
        </Space.Compact>
        <Form.Item name="description" label="描述">
          <Input.TextArea rows={2} maxLength={2000} placeholder="用途、特点，给使用的人看" />
        </Form.Item>

        <div className={styles.secTitle}>线路</div>
        <p className={styles.muted} style={{ marginTop: -4, marginBottom: 8 }}>
          优先级数字小的先用，出错（限流、过载、欠费）自动换下一条；同一优先级按权重分流。
        </p>
        <Form.List
          name="routes"
          rules={[
            {
              validator: (_, routes: RouteValues[]) =>
                routes?.length ? Promise.resolve() : Promise.reject(new Error("至少要有一条线路")),
            },
          ]}
        >
          {(fields, { add, remove }, { errors }) => (
            <>
              {fields.map((field, i) => (
                <Space key={field.key} align="start" style={{ display: "flex", marginBottom: 4 }}>
                  <Form.Item
                    name={[field.name, "account_id"]}
                    rules={[{ required: true, message: "选账号" }]}
                    style={{ width: 220, marginBottom: 8 }}
                  >
                    <Select
                      placeholder="账号"
                      loading={accountsQ.isPending}
                      onChange={() => i === 0 && void fillCaps(false)}
                      options={accounts.map((a) => ({
                        value: a.id,
                        label: `${a.name}（${a.provider_name}）${a.enabled ? "" : " · 已停用"}`,
                      }))}
                    />
                  </Form.Item>
                  <Form.Item name={[field.name, "upstream_model"]} style={{ width: 220, marginBottom: 8 }}>
                    <Input placeholder="上游模型（空 = 同模型名）" onBlur={() => i === 0 && void fillCaps(false)} />
                  </Form.Item>
                  <Form.Item name={[field.name, "priority"]} style={{ width: 90, marginBottom: 8 }}>
                    <InputNumber min={0} max={100} prefix={<span className={styles.muted}>优先</span>} style={{ width: "100%" }} />
                  </Form.Item>
                  <Form.Item name={[field.name, "weight"]} style={{ width: 100, marginBottom: 8 }}>
                    <InputNumber min={1} max={1000} prefix={<span className={styles.muted}>权重</span>} style={{ width: "100%" }} />
                  </Form.Item>
                  <Button type="text" danger disabled={fields.length <= 1} icon={<Icon name="x" size={14} />} onClick={() => remove(field.name)} />
                </Space>
              ))}
              <Button size="small" icon={<Icon name="plus" size={14} />} onClick={() => add({ priority: 2, weight: 1 })}>
                加一条线路
              </Button>
              <Form.ErrorList errors={errors} />
            </>
          )}
        </Form.List>

        <div className={styles.secTitle} style={{ display: "flex", alignItems: "center" }}>
          能力与价格
          <Button size="small" type="link" onClick={() => void fillCaps(true)}>
            按第一条线路的预置带出
          </Button>
        </div>
        {suggestNote && <p className={styles.muted} style={{ marginTop: -4, marginBottom: 8 }}>{suggestNote}</p>}
        <Space.Compact block style={{ gap: 12 }}>
          <Form.Item name={["caps", "context_window"]} label="上下文窗口（token）" style={{ flex: 1 }}>
            <InputNumber min={1} style={{ width: "100%" }} placeholder="如 131072" />
          </Form.Item>
          <Form.Item name={["caps", "max_output"]} label="最大输出（token）" style={{ flex: 1 }}>
            <InputNumber min={1} style={{ width: "100%" }} placeholder="如 8192" />
          </Form.Item>
        </Space.Compact>
        {kind === "chat" && (
          <Space size={24} wrap style={{ marginBottom: 12 }}>
            <Form.Item name={["caps", "vision"]} valuePropName="checked" noStyle>
              <Checkbox>能看图</Checkbox>
            </Form.Item>
            <Form.Item name={["caps", "tools"]} valuePropName="checked" noStyle>
              <Checkbox>工具调用</Checkbox>
            </Form.Item>
            <Form.Item name={["caps", "thinking"]} label="思考" style={{ marginBottom: 0 }}>
              <Radio.Group size="small">
                <Radio.Button value="none">不支持</Radio.Button>
                <Radio.Button value="optional">可开可关</Radio.Button>
                <Radio.Button value="always_on">一直开着</Radio.Button>
              </Radio.Group>
            </Form.Item>
          </Space>
        )}
        <Space.Compact block style={{ gap: 12 }}>
          <Form.Item name={["price", "input"]} label="输入（元 / 百万 token）" style={{ flex: 1 }}>
            <InputNumber min={0} step={0.1} style={{ width: "100%" }} placeholder="不填 = 不计费" />
          </Form.Item>
          <Form.Item name={["price", "cache_hit"]} label="缓存命中" style={{ flex: 1 }}>
            <InputNumber min={0} step={0.1} style={{ width: "100%" }} placeholder="不填 = 0" />
          </Form.Item>
          <Form.Item name={["price", "output"]} label="输出" style={{ flex: 1 }}>
            <InputNumber min={0} step={0.1} style={{ width: "100%" }} />
          </Form.Item>
        </Space.Compact>
      </Form>

      {isNew && visibility === "private" && (
        <>
          <div className={styles.secTitle}>授权（添加后也可以改）</div>
          <div style={{ marginBottom: 8 }}>
            {grants.length ? (
              grants.map((g) => (
                <Tag key={`${g.subject_type}:${g.subject_id}`} closable onClose={() => setGrants((gs) => gs.filter((x) => x !== g))}>
                  {g.label}
                </Tag>
              ))
            ) : (
              <span className={styles.muted}>还没有授权：除管理员外没人能用这个模型</span>
            )}
          </div>
          <GrantPicker
            onAdd={(g) =>
              setGrants((gs) => [...gs.filter((x) => !(x.subject_type === g.subject_type && x.subject_id === g.subject_id)), g])
            }
          />
        </>
      )}
    </Modal>
  );
}

export function GrantsModal({ model: m, onClose }: { model: MgModel; onClose: () => void }) {
  const { message } = AntdApp.useApp();
  const { add, revoke } = useMgGrantActions(m.id);
  const grants = (m.grants ?? []).filter((g) => g.permission === "READ");

  return (
    <Modal open width={640} title={`授权 · ${m.display_name}`} footer={null} onCancel={onClose}>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="授权给人或部门后，他们才能在自己的 Key 里选这个模型；收回后随之失效。"
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
                    onSuccess: () => message.success("已收回"),
                    onError: (e) => message.error(e.message),
                  })
                }
              >
                收回
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
      onAdd({ subject_type: "dept", subject_id: target, include_sub: includeSub, label: `${name}${includeSub ? "（含下级）" : ""}` });
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
