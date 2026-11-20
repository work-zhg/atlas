"use client";

/**
 * 运营账号：不在用户中心的任何租户里。只有两个角色：运营管理员（全部运营操作）、运营观察员（只读）。
 * 新账号、重置密码给一次性临时密码，首次登录必须改。至少保留一个可用的运营管理员。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Form, Input, Modal, Select, Space, Table, Tag } from "antd";
import { useState } from "react";

import { ErrorBox, OneTimeSecret, PageHead, errorText } from "@/components/common";
import { api } from "@/lib/api";
import { dateTime } from "@/lib/format";
import { useMe } from "@/lib/session";
import type { Operator, Role } from "@/lib/types";

const ROLE_OPTIONS = [
  { value: "ADMIN", label: "运营管理员" },
  { value: "OBSERVER", label: "运营观察员（只读）" },
];

export default function OperatorsPage() {
  const { data: me } = useMe();
  const admin = me?.role === "ADMIN";
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const q = useQuery({ queryKey: ["operators"], queryFn: () => api.get<Operator[]>("/operators") });
  const [creating, setCreating] = useState(false);
  const [secret, setSecret] = useState<{ account: string; value: string } | null>(null);

  async function act(fn: () => Promise<unknown>, ok: string) {
    try {
      await fn();
      message.success(ok);
      await qc.invalidateQueries({ queryKey: ["operators"] });
    } catch (e) {
      message.error(errorText(e));
    }
  }

  return (
    <>
      <PageHead
        title="运营账号"
        sub="运营人员不属于任何租户。一个人同时是某个租户的员工也没关系：两个账号、分开登录"
        extra={
          admin && (
            <Button type="primary" onClick={() => setCreating(true)}>
              开账号
            </Button>
          )
        }
      />
      {secret && (
        <div style={{ marginBottom: 16 }}>
          <OneTimeSecret label={`${secret.account} 的临时密码`} value={secret.value} />
        </div>
      )}
      <ErrorBox error={q.error} />
      <Table<Operator>
        rowKey="id"
        loading={q.isLoading}
        dataSource={q.data ?? []}
        pagination={false}
        columns={[
          { title: "账号", dataIndex: "account" },
          { title: "名字", dataIndex: "name" },
          {
            title: "角色",
            render: (_, o) =>
              admin && o.id !== me?.id ? (
                <Select
                  size="small"
                  value={o.role}
                  options={ROLE_OPTIONS}
                  style={{ width: 170 }}
                  onChange={(role: Role) => act(() => api.patch(`/operators/${o.id}`, { role }), "角色已改")}
                />
              ) : (
                ROLE_OPTIONS.find((r) => r.value === o.role)?.label
              ),
          },
          {
            title: "状态",
            render: (_, o) => (
              <Space size={4}>
                <Tag color={o.status === "active" ? "green" : "default"}>{o.status === "active" ? "正常" : "已停用"}</Tag>
                {o.locked && <Tag color="red">暂时锁定</Tag>}
                {o.must_change_password && <Tag>待改临时密码</Tag>}
              </Space>
            ),
          },
          { title: "最近登录", dataIndex: "last_login_at", render: dateTime },
          {
            title: "",
            render: (_, o) =>
              admin &&
              o.id !== me?.id && (
                <Space size={0}>
                  <Button
                    type="link"
                    size="small"
                    onClick={() =>
                      modal.confirm({
                        title: `重置 ${o.account} 的密码`,
                        content: "他的全部登录作废，锁定解除，给一个新的临时密码。",
                        onOk: async () => {
                          try {
                            const out = await api.post<{ temp_password: string }>(`/operators/${o.id}/reset-password`);
                            setSecret({ account: o.account, value: out.temp_password });
                            await qc.invalidateQueries({ queryKey: ["operators"] });
                          } catch (e) {
                            message.error(errorText(e));
                          }
                        },
                      })
                    }
                  >
                    重置密码
                  </Button>
                  {o.status === "active" ? (
                    <Button type="link" size="small" danger onClick={() => act(() => api.post(`/operators/${o.id}/disable`), "已停用")}>
                      停用
                    </Button>
                  ) : (
                    <Button type="link" size="small" onClick={() => act(() => api.post(`/operators/${o.id}/enable`), "已启用")}>
                      启用
                    </Button>
                  )}
                </Space>
              ),
          },
        ]}
      />
      {creating && (
        <CreateOperator
          onClose={() => setCreating(false)}
          onCreated={(account, value) => {
            setSecret({ account, value });
            void qc.invalidateQueries({ queryKey: ["operators"] });
          }}
        />
      )}
    </>
  );
}

function CreateOperator({ onClose, onCreated }: { onClose: () => void; onCreated: (account: string, temp: string) => void }) {
  const [form] = Form.useForm();
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  return (
    <Modal open title="开运营账号" onCancel={onClose} onOk={() => form.submit()} confirmLoading={busy}>
      <ErrorBox error={error} />
      <Form
        form={form}
        layout="vertical"
        requiredMark={false}
        initialValues={{ role: "OBSERVER" }}
        onFinish={async (v) => {
          setBusy(true);
          try {
            const out = await api.post<{ operator: Operator; temp_password: string }>("/operators", v);
            onCreated(out.operator.account, out.temp_password);
            onClose();
          } catch (e) {
            setError(e);
          } finally {
            setBusy(false);
          }
        }}
      >
        <Form.Item name="account" label="账号" extra="小写字母、数字、点和下划线，3–32 位" rules={[{ required: true }]}>
          <Input />
        </Form.Item>
        <Form.Item name="name" label="名字" rules={[{ required: true }]}>
          <Input />
        </Form.Item>
        <Form.Item name="role" label="角色">
          <Select options={ROLE_OPTIONS} />
        </Form.Item>
      </Form>
    </Modal>
  );
}
