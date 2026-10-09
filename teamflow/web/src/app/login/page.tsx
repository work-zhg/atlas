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
      await api.post("/auth/login", values);
      await qc.invalidateQueries();
      router.replace("/");
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
          background: "linear-gradient(135deg,#4f46e5,#0ea5e9)",
          color: "#fff",
          padding: 48,
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
        }}
      >
        <div style={{ fontSize: 15, opacity: 0.85, marginBottom: 12 }}>🧩 AI TeamFlow</div>
        <h2 style={{ fontSize: 30, margin: 0 }}>让 AI Agent 作为团队成员参与研发流程</h2>
        <p style={{ opacity: 0.85, fontSize: 15, maxWidth: 480 }}>
          团队由人和 Agent 组成；流程模板定义怎么做，项目把角色分配给人和 Agent，人下达指令、评审成果，Agent 执行。
        </p>
      </div>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center" }}>
        <Card style={{ width: 360 }} title="登录 AI TeamFlow">
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
            <div className="tf-muted" style={{ fontSize: 12, marginTop: 12 }}>
              使用用户中心的账号密码登录
            </div>
          </Form>
        </Card>
      </div>
    </div>
  );
}
