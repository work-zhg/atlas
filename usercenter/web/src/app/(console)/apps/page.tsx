"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, Form, Input, Modal, Tag } from "antd";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { PageHead, showOnce } from "@/components/common";
import { api, errorText } from "@/lib/api";
import { useCan } from "@/lib/session";
import type { AppInfo } from "@/lib/types";

export default function AppsPage() {
  const can = useCan();
  const router = useRouter();
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();
  const list = useQuery({ queryKey: ["apps"], queryFn: () => api.get<AppInfo[]>("/apps") });

  async function create(v: { name: string; description?: string; icon?: string }) {
    try {
      const r = await api.post<{ app: AppInfo; app_secret: string }>("/apps", v);
      qc.invalidateQueries({ queryKey: ["apps"] });
      setOpen(false);
      showOnce(modal, "应用已接入，请保存 App Secret", [["App Key", r.app.app_key ?? ""], ["App Secret", r.app_secret]], "App Secret 只显示这一次，只能保存在应用后端；新应用默认无人可访问，请设置可访问范围。");
      router.push(`/apps/${r.app.id}`);
    } catch (e) {
      message.error(errorText(e));
    }
  }

  return (
    <>
      <PageHead
        title="应用接入"
        sub="接入的系统在服务端用 App Key / Secret 调用开放接口，获取用户、组织与本应用的权限；角色、菜单、操作、数据编码都属于应用。"
        extra={can("app:manage") && <Button type="primary" onClick={() => (form.resetFields(), setOpen(true))}>＋ 接入应用</Button>}
      />
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(320px,1fr))", gap: 16 }}>
        {(list.data ?? []).map((a) => (
          <Card key={a.id} hoverable onClick={() => router.push(`/apps/${a.id}`)} loading={list.isLoading}>
            <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
              <span style={{ fontSize: 28 }}>{a.icon}</span>
              <div>
                <b style={{ fontSize: 15 }}>{a.name}</b>
                <div className="uc-muted" style={{ fontSize: 12.5 }}>{a.description}</div>
              </div>
              <div className="uc-grow" />
              {a.is_builtin ? <Tag color="purple">内置</Tag> : a.status === "active" ? <Tag color="green">已启用</Tag> : <Tag>已停用</Tag>}
            </div>
            <div className="uc-muted" style={{ marginTop: 12, fontSize: 12.5, lineHeight: 1.9 }}>
              {!a.is_builtin && a.app_key && (
                <div>
                  App Key <span className="uc-mono">{a.app_key}</span>
                </div>
              )}
              {!a.is_builtin && a.accessible_users !== undefined && (
                <div>可访问：{a.scope_all ? "全员" : a.scope_dept_ids?.length ? `指定部门 · ${a.accessible_users} 人` : "未设置（无人可访问）"}</div>
              )}
              <div>
                {a.operations} 个操作 · {a.menus} 个菜单 · {a.roles} 个角色{!a.is_builtin && ` · ${a.data_types} 个数据编码`}
              </div>
            </div>
          </Card>
        ))}
      </div>
      <Modal open={open} title="接入应用" onCancel={() => setOpen(false)} onOk={() => form.submit()} okText="创建">
        <Form form={form} layout="vertical" onFinish={create}>
          <Form.Item name="name" label="应用名称" rules={[{ required: true }]}>
            <Input maxLength={50} placeholder="例如：报销系统" />
          </Form.Item>
          <Form.Item name="description" label="说明">
            <Input maxLength={200} />
          </Form.Item>
          <Form.Item name="icon" label="图标（emoji）">
            <Input maxLength={4} placeholder="🧱" />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}
