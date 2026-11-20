"use client";

/** 运营账号登录（账号 + 密码）。运营账号不在用户中心的任何租户里，和租户的登录完全无关。 */
import { useQueryClient } from "@tanstack/react-query";
import { Button, Card, Form, Input, Typography } from "antd";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { ErrorBox } from "@/components/common";
import { api } from "@/lib/api";
import type { Me } from "@/lib/types";

export default function LoginPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(v: { account: string; password: string }) {
    setBusy(true);
    setError(null);
    try {
      const me = await api.post<Me>("/auth/login", v);
      qc.setQueryData(["me"], me);
      router.replace(me.must_change_password ? "/password" : "/tenants");
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ minHeight: "100vh", display: "grid", placeItems: "center", background: "#1f1a0a" }}>
      <Card style={{ width: 380 }}>
        <Typography.Title level={4} style={{ marginTop: 0 }}>
          🛠️ 运营中心
        </Typography.Title>
        <Typography.Paragraph type="secondary">用运营账号登录。运营账号和任何租户里的账号都无关。</Typography.Paragraph>
        <ErrorBox error={error} />
        <Form layout="vertical" onFinish={submit} requiredMark={false}>
          <Form.Item name="account" label="账号" rules={[{ required: true, message: "请输入账号" }]}>
            <Input autoFocus autoComplete="username" />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true, message: "请输入密码" }]}>
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={busy}>
            登录
          </Button>
        </Form>
      </Card>
    </div>
  );
}
