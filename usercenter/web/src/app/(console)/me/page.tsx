"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Avatar, Button, Card, Empty, Form, Input } from "antd";
import { useState } from "react";

import { PageHead } from "@/components/common";
import { api, errorText } from "@/lib/api";
import { useMe } from "@/lib/session";
import type { AppInfo } from "@/lib/types";

export default function MePage() {
  const { data: me } = useMe();
  const qc = useQueryClient();
  const { message } = App.useApp();
  const apps = useQuery({ queryKey: ["me", "apps"], queryFn: () => api.get<AppInfo[]>("/me/apps") });
  const [pwErr, setPwErr] = useState<string>();
  const [pwForm] = Form.useForm();
  if (!me) return <Card loading />;

  async function saveInfo(v: { email: string; phone: string }) {
    try {
      await api.patch("/me", { email: v.email, phone: v.phone || null });
      qc.invalidateQueries({ queryKey: ["me"] });
      message.success("已保存");
    } catch (e) {
      message.error(errorText(e));
    }
  }

  async function changePw(v: { old_password: string; new_password: string; confirm: string }) {
    setPwErr(undefined);
    if (v.new_password !== v.confirm) return setPwErr("两次输入的新密码不一致");
    try {
      await api.post("/auth/password", { old_password: v.old_password, new_password: v.new_password });
      pwForm.resetFields();
      message.success("密码已修改；其他设备上的登录已注销");
    } catch (e) {
      setPwErr(errorText(e));
    }
  }

  return (
    <>
      <PageHead title="个人中心" sub="查看个人信息、修改密码；可访问的应用列表。" />
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, alignItems: "start" }}>
        <Card title="个人信息">
          <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 16 }}>
            <Avatar size={56} style={{ background: "#14b8a6", fontSize: 22 }}>
              {me.user.name.slice(-1)}
            </Avatar>
            <div>
              <b style={{ fontSize: 17 }}>{me.user.name}</b>
              <div className="uc-muted">
                {me.user.account} · {me.user.dept_path.join(" / ")}
              </div>
              {me.manager && <div className="uc-muted">直属上级：{me.manager.name}</div>}
            </div>
          </div>
          <Form layout="vertical" initialValues={{ email: me.user.email, phone: me.user.phone ?? "" }} onFinish={saveInfo}>
            <Form.Item name="email" label="邮箱" rules={[{ required: true, type: "email" }]}>
              <Input />
            </Form.Item>
            <Form.Item name="phone" label="手机" rules={[{ pattern: /^(1\d{10})?$/, message: "11 位手机号" }]}>
              <Input />
            </Form.Item>
            <div className="uc-muted" style={{ marginBottom: 12 }}>
              姓名、部门、岗位由管理员维护。
            </div>
            <Button type="primary" htmlType="submit">
              保存
            </Button>
          </Form>
        </Card>
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Card title="修改密码">
            {pwErr && <Alert type="error" showIcon message={pwErr} style={{ marginBottom: 12 }} />}
            <Form form={pwForm} layout="vertical" onFinish={changePw}>
              <Form.Item name="old_password" label="当前密码" rules={[{ required: true }]}>
                <Input.Password />
              </Form.Item>
              <Form.Item name="new_password" label="新密码" extra={`要求：${me.password_policy}`} rules={[{ required: true }]}>
                <Input.Password />
              </Form.Item>
              <Form.Item name="confirm" label="确认新密码" rules={[{ required: true }]}>
                <Input.Password />
              </Form.Item>
              <Button type="primary" htmlType="submit">
                修改密码
              </Button>
            </Form>
          </Card>
          <Card title="我可以访问的应用">
            {(apps.data ?? []).length ? (
              apps.data!.map((a) => (
                <div key={a.id} style={{ padding: "6px 0" }}>
                  {a.icon} <b>{a.name}</b> <span className="uc-muted">{a.description}</span>
                </div>
              ))
            ) : (
              <Empty description="没有可访问的应用" />
            )}
          </Card>
        </div>
      </div>
    </>
  );
}
