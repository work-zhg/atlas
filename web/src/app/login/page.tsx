"use client";

/** 用户中心账号登录（Atlas 代为向用户中心校验密码，权限设计 §4.1）。 */
import { useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Form, Input, Spin, Typography } from "antd";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { ApiError, http } from "@/lib/http";
import { ME_KEY, type Me } from "@/lib/session";

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const qc = useQueryClient();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  async function submit(v: { account: string; password: string }) {
    setBusy(true);
    setError(undefined);
    try {
      const me = await http.post<Me>("/v1/auth/login", v);
      qc.setQueryData(ME_KEY, me);
      const next = params.get("next");
      router.replace(next && next.startsWith("/") && !next.startsWith("/login") ? next : "/chat");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card style={{ width: 380 }}>
      <Typography.Title level={4} style={{ marginTop: 0 }}>
        登录 Atlas
      </Typography.Title>
      <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
        使用用户中心的账号登录
      </Typography.Paragraph>
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
      <Form layout="vertical" requiredMark={false} onFinish={submit}>
        <Form.Item name="account" label="账号" rules={[{ required: true, message: "请输入账号" }]}>
          <Input autoComplete="username" autoFocus />
        </Form.Item>
        <Form.Item name="password" label="密码" rules={[{ required: true, message: "请输入密码" }]}>
          <Input.Password autoComplete="current-password" />
        </Form.Item>
        <Button type="primary" htmlType="submit" block loading={busy}>
          登录
        </Button>
      </Form>
    </Card>
  );
}

export default function LoginPage() {
  return (
    <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
      <Suspense fallback={<Spin />}>
        <LoginForm />
      </Suspense>
    </div>
  );
}
