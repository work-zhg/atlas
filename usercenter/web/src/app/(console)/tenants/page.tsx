"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Drawer, Form, Input, InputNumber, Modal, Select, Space, Switch, Table, Tag } from "antd";
import { useState } from "react";

import { PageHead, showOnce } from "@/components/common";
import { api, errorText } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { useCan } from "@/lib/session";
import type { TenantInfo } from "@/lib/types";

const STATUS: Record<TenantInfo["status"], { label: string; color: string }> = {
  active: { label: "正常", color: "green" },
  suspended: { label: "已停用", color: "orange" },
  closed: { label: "已注销", color: "default" },
};

interface TenantApp {
  app: { id: string; name: string; icon: string | null; status: string };
  enabled: boolean;
  scope_all: boolean;
  enabled_at: string | null;
}

/** 租户管理（平台级，租户设计 §07）：建租户、停用 / 到期、配额、给租户开通应用。 */
export default function TenantsPage() {
  const can = useCan();
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const list = useQuery({ queryKey: ["tenants"], queryFn: () => api.get<TenantInfo[]>("/tenants") });
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<TenantInfo | null>(null);
  const [appsOf, setAppsOf] = useState<TenantInfo | null>(null);
  const [form] = Form.useForm();
  const manage = can("tenant:manage");
  const refresh = () => qc.invalidateQueries({ queryKey: ["tenants"] });

  async function create(v: { code: string; name: string; max_users?: number; account: string; admin_name: string; email: string }) {
    try {
      const r = await api.post<{ tenant: TenantInfo; admin: { account: string }; temp_password: string }>("/tenants", {
        code: v.code,
        name: v.name,
        max_users: v.max_users ?? null,
        admin: { account: v.account, name: v.admin_name, email: v.email },
      });
      refresh();
      setCreating(false);
      showOnce(
        modal,
        "租户已创建，请把登录信息交给租户管理员",
        [
          ["企业代码", r.tenant.code],
          ["账号", r.admin.account],
          ["临时密码", r.temp_password],
        ],
        "临时密码只显示这一次；首次登录须修改。应用还没有开通，请在「开通应用」里为该租户开通。",
      );
    } catch (e) {
      message.error(errorText(e));
    }
  }

  return (
    <>
      <PageHead
        title="租户管理"
        sub="每个租户是一个相互隔离的组织空间：自己的部门、用户、授权与数据权限。平台管理员在这里只管理租户本身与开通的应用，看不到租户内的人员与授权。"
        extra={manage && <Button type="primary" onClick={() => (form.resetFields(), setCreating(true))}>＋ 新建租户</Button>}
      />
      <Table<TenantInfo>
        rowKey="id"
        loading={list.isLoading}
        dataSource={list.data ?? []}
        pagination={false}
        columns={[
          {
            title: "租户",
            render: (_, t) => (
              <Space>
                <b>{t.name}</b>
                {t.kind === "platform" && <Tag color="purple">平台</Tag>}
              </Space>
            ),
          },
          { title: "企业代码", render: (_, t) => <span className="uc-mono">{t.code}</span> },
          {
            title: "状态",
            render: (_, t) => (
              <Space>
                <Tag color={STATUS[t.status].color}>{STATUS[t.status].label}</Tag>
                {t.status === "active" && !t.usable && <Tag color="red">已到期</Tag>}
              </Space>
            ),
          },
          { title: "人数 / 配额", render: (_, t) => `${t.users} / ${t.max_users ?? "不限"}` },
          { title: "到期", render: (_, t) => (t.expires_at ? fmtTime(t.expires_at) : "—") },
          { title: "创建时间", render: (_, t) => <span className="uc-muted">{fmtTime(t.created_at)}</span> },
          {
            title: "",
            align: "right",
            render: (_, t) => (
              <Space size={0}>
                <Button type="link" size="small" onClick={() => setAppsOf(t)}>
                  开通应用
                </Button>
                {manage && t.kind !== "platform" && (
                  <Button type="link" size="small" onClick={() => setEditing(t)}>
                    编辑
                  </Button>
                )}
              </Space>
            ),
          },
        ]}
      />
      <Modal open={creating} title="新建租户" onCancel={() => setCreating(false)} onOk={() => form.submit()} okText="创建" width={560}>
        <Form form={form} layout="vertical" onFinish={create}>
          <Form.Item name="name" label="租户名称" rules={[{ required: true }]} extra="同时作为租户根部门的名称">
            <Input maxLength={64} placeholder="例如：星河制造" />
          </Form.Item>
          <Form.Item
            name="code"
            label="企业代码"
            extra="登录时填写；小写字母开头，小写字母、数字与 -，创建后不可改"
            rules={[{ required: true, pattern: /^[a-z][a-z0-9-]{1,31}$/, message: "格式不正确" }]}
          >
            <Input maxLength={32} placeholder="例如：xinghe" />
          </Form.Item>
          <Form.Item name="max_users" label="人员配额" extra="未停用的人数上限；不填 = 不限">
            <InputNumber min={1} style={{ width: "100%" }} />
          </Form.Item>
          <div className="uc-muted" style={{ margin: "4px 0 8px" }}>
            首个租户管理员（超级管理员）
          </div>
          <Space style={{ display: "flex" }} align="start">
            <Form.Item name="account" label="账号" rules={[{ required: true, pattern: /^[a-z0-9._]{3,32}$/, message: "3–32 位小写字母、数字、. 或 _" }]}>
              <Input />
            </Form.Item>
            <Form.Item name="admin_name" label="姓名" rules={[{ required: true }]}>
              <Input />
            </Form.Item>
          </Space>
          <Form.Item name="email" label="邮箱" rules={[{ required: true, type: "email" }]}>
            <Input />
          </Form.Item>
        </Form>
      </Modal>
      {editing && <EditTenant tenant={editing} onClose={() => setEditing(null)} onDone={refresh} />}
      <Drawer open={!!appsOf} title={appsOf ? `开通应用 · ${appsOf.name}` : ""} width={520} onClose={() => setAppsOf(null)}>
        {appsOf && <TenantApps tenant={appsOf} manage={manage} />}
      </Drawer>
    </>
  );
}

/** ISO 时间 ↔ <input type="datetime-local"> 的本地时间串 */
const toLocalInput = (iso: string | null) => {
  if (!iso) return "";
  const d = new Date(iso);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
};

function EditTenant({ tenant, onClose, onDone }: { tenant: TenantInfo; onClose: () => void; onDone: () => void }) {
  const { message } = App.useApp();
  const [form] = Form.useForm();
  async function save(v: { name: string; status: TenantInfo["status"]; max_users?: number | null; expires_at?: string }) {
    try {
      await api.patch(`/tenants/${tenant.id}`, {
        name: v.name,
        status: v.status,
        max_users: v.max_users ?? null,
        expires_at: v.expires_at ? new Date(v.expires_at).toISOString() : null,
      });
      onDone();
      message.success("已保存");
      onClose();
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <Modal open title={`编辑租户 · ${tenant.name}`} onCancel={onClose} onOk={() => form.submit()} okText="保存">
      <Form
        form={form}
        layout="vertical"
        onFinish={save}
        initialValues={{
          name: tenant.name,
          status: tenant.status,
          max_users: tenant.max_users,
          expires_at: toLocalInput(tenant.expires_at),
        }}
      >
        <Form.Item name="name" label="租户名称" rules={[{ required: true }]}>
          <Input maxLength={64} />
        </Form.Item>
        <Form.Item name="status" label="状态" extra="停用 / 注销后该租户的人无法登录，接口令牌立即失效；数据保留">
          <Select
            options={[
              { value: "active", label: "正常" },
              { value: "suspended", label: "停用" },
              { value: "closed", label: "注销" },
            ]}
          />
        </Form.Item>
        <Form.Item name="max_users" label="人员配额" extra="不填 = 不限">
          <InputNumber min={1} style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item name="expires_at" label="到期时间" extra="到期后视同停用；不填 = 不到期">
          <Input type="datetime-local" />
        </Form.Item>
      </Form>
    </Modal>
  );
}

function TenantApps({ tenant, manage }: { tenant: TenantInfo; manage: boolean }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const key = ["tenant-apps", tenant.id];
  const q = useQuery({ queryKey: key, queryFn: () => api.get<TenantApp[]>(`/tenants/${tenant.id}/apps`) });
  async function toggle(appId: string, enabled: boolean) {
    try {
      await api.put(`/tenants/${tenant.id}/apps/${appId}`, { enabled });
      await qc.invalidateQueries({ queryKey: key });
      message.success(enabled ? "已开通" : "已关闭");
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <>
      <div className="uc-muted" style={{ marginBottom: 12 }}>
        开通后由租户管理员在「应用接入」里设置可访问范围（默认无人可访问）。关闭后该租户的接口令牌立即失效。
      </div>
      <Table<TenantApp>
        rowKey={(r) => r.app.id}
        size="small"
        loading={q.isLoading}
        pagination={false}
        dataSource={q.data ?? []}
        columns={[
          {
            title: "应用",
            render: (_, r) => (
              <Space>
                <span>{r.app.icon}</span>
                <b>{r.app.name}</b>
                {r.app.status !== "active" && <Tag>应用已停用</Tag>}
              </Space>
            ),
          },
          { title: "范围", render: (_, r) => (r.enabled ? (r.scope_all ? "全员" : "租户自定") : "—") },
          {
            title: "开通",
            align: "right",
            render: (_, r) => <Switch checked={r.enabled} disabled={!manage} onChange={(v) => toggle(r.app.id, v)} />,
          },
        ]}
      />
    </>
  );
}
