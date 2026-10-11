"use client";

/**
 * 服务账号（权限设计 §10）：后端服务（如 AI TeamFlow）调用 Atlas 的主体。
 * 密钥只在创建 / 轮换时显示一次；服务账号的会话默认关闭用户记忆。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Form, Input, Modal, Radio, Space, Switch, Table, Tag, Typography } from "antd";
import { useState } from "react";

import { http } from "@/lib/http";

interface ServiceAccount {
  id: string;
  name: string;
  description: string;
  key_prefix: string;
  operations: string[];
  agent_scope: "all_enabled" | "list";
  agent_ids: string[];
  allow_acting: boolean;
  memory_default: "on" | "off";
  status: "active" | "disabled";
  created_at: string;
  last_used_at: string | null;
  key?: string | null;
}

const KEY = ["service-accounts"];

export default function ServiceAccountsPage() {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [creating, setCreating] = useState(false);
  const [secret, setSecret] = useState<{ name: string; key: string }>();
  const { data = [], isPending, error } = useQuery({
    queryKey: KEY,
    queryFn: () => http.get<ServiceAccount[]>("/v1/service-accounts"),
  });

  async function act(fn: () => Promise<ServiceAccount>, done?: (sa: ServiceAccount) => void) {
    try {
      const sa = await fn();
      await qc.invalidateQueries({ queryKey: KEY });
      done?.(sa);
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  return (
    <div style={{ flex: 1, overflow: "auto", padding: "24px 28px" }}>
      <Space style={{ width: "100%", justifyContent: "space-between", marginBottom: 12 }}>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>
            服务账号
          </Typography.Title>
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            后端服务调用 Atlas 用的凭据。服务账号建的会话归服务账号，默认关闭用户记忆。
          </Typography.Text>
        </div>
        <Button type="primary" onClick={() => setCreating(true)}>
          新建服务账号
        </Button>
      </Space>
      {error && <Alert type="error" showIcon message={(error as Error).message} />}
      <Table<ServiceAccount>
        rowKey="id"
        loading={isPending}
        dataSource={data}
        pagination={false}
        size="middle"
        columns={[
          {
            title: "名称",
            render: (_, r) => (
              <Space direction="vertical" size={0}>
                <b>{r.name}</b>
                {r.description && <span style={{ color: "var(--fg-3)", fontSize: 12 }}>{r.description}</span>}
              </Space>
            ),
          },
          { title: "密钥", render: (_, r) => <code>{r.key_prefix}…</code> },
          {
            title: "可用智能体",
            render: (_, r) => (r.agent_scope === "all_enabled" ? "全部已启用" : `指定 ${r.agent_ids.length} 个`),
          },
          { title: "代表用户", render: (_, r) => (r.allow_acting ? "允许" : "不允许") },
          { title: "用户记忆", render: (_, r) => (r.memory_default === "on" ? "开启" : "关闭") },
          {
            title: "状态",
            render: (_, r) => (r.status === "active" ? <Tag color="green">启用</Tag> : <Tag>已停用</Tag>),
          },
          { title: "最近使用", render: (_, r) => (r.last_used_at ? new Date(r.last_used_at).toLocaleString() : "—") },
          {
            title: "",
            render: (_, r) => (
              <Space>
                <Button
                  size="small"
                  onClick={() =>
                    Modal.confirm({
                      title: `轮换「${r.name}」的密钥？`,
                      content: "旧密钥立即失效，使用它的服务需要换成新密钥。",
                      onOk: () =>
                        act(
                          () => http.post<ServiceAccount>(`/v1/service-accounts/${r.id}/rotate-key`),
                          (sa) => setSecret({ name: sa.name, key: sa.key ?? "" }),
                        ),
                    })
                  }
                >
                  轮换密钥
                </Button>
                <Button
                  size="small"
                  danger={r.status === "active"}
                  onClick={() =>
                    act(() =>
                      http.patch<ServiceAccount>(`/v1/service-accounts/${r.id}`, {
                        status: r.status === "active" ? "disabled" : "active",
                      }),
                    )
                  }
                >
                  {r.status === "active" ? "停用" : "启用"}
                </Button>
              </Space>
            ),
          },
        ]}
      />

      <CreateModal
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={(sa) => {
          setCreating(false);
          void qc.invalidateQueries({ queryKey: KEY });
          setSecret({ name: sa.name, key: sa.key ?? "" });
        }}
      />
      <Modal
        open={!!secret}
        title={`「${secret?.name}」的密钥`}
        onCancel={() => setSecret(undefined)}
        footer={<Button onClick={() => setSecret(undefined)}>我已保存</Button>}
      >
        <Alert type="warning" showIcon message="密钥只显示这一次，请立即保存到服务的配置（如 .env）中。" />
        <Typography.Paragraph copyable code style={{ marginTop: 12, wordBreak: "break-all" }}>
          {secret?.key}
        </Typography.Paragraph>
      </Modal>
    </div>
  );
}

function CreateModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (sa: ServiceAccount) => void;
}) {
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [busy, setBusy] = useState(false);

  async function submit() {
    const v = await form.validateFields();
    setBusy(true);
    try {
      onCreated(await http.post<ServiceAccount>("/v1/service-accounts", v));
      form.resetFields();
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} title="新建服务账号" onOk={submit} onCancel={onClose} confirmLoading={busy} destroyOnHidden>
      <Form
        form={form}
        layout="vertical"
        initialValues={{ agent_scope: "all_enabled", allow_acting: false, memory_default: "off" }}
      >
        <Form.Item name="name" label="名称" rules={[{ required: true, message: "请填写名称" }]}>
          <Input maxLength={128} placeholder="如：teamflow" />
        </Form.Item>
        <Form.Item name="description" label="说明">
          <Input.TextArea maxLength={2000} rows={2} />
        </Form.Item>
        <Form.Item name="agent_scope" label="可用智能体">
          <Radio.Group options={[{ value: "all_enabled", label: "全部已启用的智能体" }]} />
        </Form.Item>
        <Form.Item
          name="allow_acting"
          label="允许代表用户"
          valuePropName="checked"
          extra="请求带 X-Atlas-Acting-User 时记入审计（这一轮是谁触发的），不扩大任何访问。"
        >
          <Switch />
        </Form.Item>
        <Form.Item name="memory_default" label="用户记忆">
          <Radio.Group
            options={[
              { value: "off", label: "关闭（推荐：同一服务身份下的不同团队数据不会互相串）" },
              { value: "on", label: "开启" },
            ]}
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}
