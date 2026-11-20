"use client";

/** 改密码：临时密码登录后必须先改；之后也可以随时改（别处的登录作废，这一个保留）。 */
import { useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, Form, Input, Typography } from "antd";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { ErrorBox } from "@/components/common";
import { api } from "@/lib/api";
import { useMe } from "@/lib/session";

export default function PasswordPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const { message } = App.useApp();
  const { data: me } = useMe();
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(v: { old_password: string; new_password: string }) {
    setBusy(true);
    setError(null);
    try {
      await api.post("/auth/password", v);
      await qc.invalidateQueries({ queryKey: ["me"] });
      message.success("密码已修改");
      router.replace("/tenants");
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ minHeight: "100vh", display: "grid", placeItems: "center" }}>
      <Card style={{ width: 420 }}>
        <Typography.Title level={4} style={{ marginTop: 0 }}>
          修改密码
        </Typography.Title>
        {me?.must_change_password && (
          <Typography.Paragraph type="secondary">这是临时密码，先改成你自己的密码再继续。</Typography.Paragraph>
        )}
        <ErrorBox error={error} />
        <Form layout="vertical" onFinish={submit} requiredMark={false}>
          <Form.Item name="old_password" label="原密码" rules={[{ required: true }]}>
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <Form.Item
            name="new_password"
            label="新密码"
            extra="至少 10 位，含大写和小写字母、数字"
            rules={[{ required: true }]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={busy}>
            保存
          </Button>
        </Form>
      </Card>
    </div>
  );
}
