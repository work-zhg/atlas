"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Form, Input } from "antd";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { api, errorText } from "@/lib/api";
import { useMe } from "@/lib/session";

export default function ChangePasswordPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const { data: me } = useMe();
  const [err, setErr] = useState<string>();
  const [loading, setLoading] = useState(false);

  async function submit(v: { old_password: string; new_password: string; confirm: string }) {
    if (v.new_password !== v.confirm) {
      setErr("两次输入的新密码不一致");
      return;
    }
    setLoading(true);
    setErr(undefined);
    try {
      await api.post("/auth/password", { old_password: v.old_password, new_password: v.new_password });
      await qc.invalidateQueries({ queryKey: ["me"] });
      router.replace("/");
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center" }}>
      <Card style={{ width: 400 }} title={me?.first_login ? "首次登录，请设置新密码" : "密码需要修改"}>
        <p className="uc-muted" style={{ marginTop: 0 }}>
          {me ? `${me.user.name}（${me.user.account}）` : ""}　要求：{me?.password_policy ?? "—"}
        </p>
        {err && <Alert type="error" showIcon message={err} style={{ marginBottom: 16 }} />}
        <Form layout="vertical" onFinish={submit} requiredMark={false}>
          <Form.Item name="old_password" label="当前密码（临时密码）" rules={[{ required: true }]}>
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <Form.Item name="new_password" label="新密码" rules={[{ required: true }]}>
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <Form.Item name="confirm" label="确认新密码" rules={[{ required: true }]}>
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={loading}>
            设置并进入
          </Button>
        </Form>
      </Card>
    </div>
  );
}
