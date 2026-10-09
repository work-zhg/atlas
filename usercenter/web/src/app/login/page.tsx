"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Form, Input } from "antd";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { api, errorText } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const [err, setErr] = useState<string>();
  const [loading, setLoading] = useState(false);

  async function submit(values: { account: string; password: string }) {
    setLoading(true);
    setErr(undefined);
    try {
      const r = await api.post<{ must_change_password: boolean }>("/auth/login", values);
      await qc.invalidateQueries();
      router.replace(r.must_change_password ? "/change-password" : "/");
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div style={{ minHeight: "100vh", display: "grid", gridTemplateColumns: "1.1fr 1fr" }}>
      <div
        style={{
          background: "linear-gradient(135deg,#0ea5e9,#6366f1)",
          color: "#fff",
          padding: 48,
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
        }}
      >
        <h2 style={{ fontSize: 30, margin: 0 }}>一个账号，管理用户、组织与权限</h2>
        <p style={{ opacity: 0.85, fontSize: 15, maxWidth: 460 }}>
          用户与组织在这里统一维护；接入的内部系统通过开放接口获取用户、部门与权限。
        </p>
      </div>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center" }}>
        <Card style={{ width: 360 }} title="登录用户中心">
          {err && <Alert type="error" showIcon message={err} style={{ marginBottom: 16 }} />}
          <Form layout="vertical" onFinish={submit} requiredMark={false}>
            <Form.Item name="account" label="账号" rules={[{ required: true, message: "请输入账号" }]}>
              <Input autoComplete="username" autoFocus />
            </Form.Item>
            <Form.Item name="password" label="密码" rules={[{ required: true, message: "请输入密码" }]}>
              <Input.Password autoComplete="current-password" />
            </Form.Item>
            <Button type="primary" htmlType="submit" block loading={loading}>
              登录
            </Button>
          </Form>
        </Card>
      </div>
    </div>
  );
}
