"use client";

/**
 * 运营账号登录（账号 + 密码）。运营账号不在用户中心的任何租户里，和企业的登录完全无关。
 *
 * ★ 临时密码登录后，在同一张卡片里第二步设新密码（同用户中心），刚输的临时密码只在内存里带过去。
 */
import { useQueryClient } from "@tanstack/react-query";
import { Button, Form, Input } from "antd";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { AuthCard, AuthError, SetPasswordForm } from "@/components/auth/AuthCard";
import styles from "@/components/auth/auth.module.css";
import { api } from "@/lib/api";
import type { Me } from "@/lib/types";

export default function LoginPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<{ me: Me; password: string } | null>(null);

  async function submit(v: { account: string; password: string }) {
    setBusy(true);
    setError(null);
    try {
      const me = await api.post<Me>("/auth/login", v);
      qc.setQueryData(["me"], me);
      if (me.must_change_password) setPending({ me, password: v.password });
      else router.replace("/tenants");
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  if (pending)
    return (
      <AuthCard>
        <SetPasswordForm
          me={pending.me}
          oldPassword={pending.password}
          onDone={() => router.replace("/tenants")}
          onBack={() => {
            setPending(null);
            void api.post("/auth/logout");
          }}
        />
      </AuthCard>
    );

  return (
    <AuthCard>
      <h1 className={styles.title}>登录</h1>
      <p className={styles.sub}>平台运营专用。这里能开通、停用所有企业，请用自己的账号登录</p>
      {!!error && <AuthError error={error} />}
      <Form layout="vertical" onFinish={submit} requiredMark={false}>
        <Form.Item name="account" label="账号" rules={[{ required: true, message: "请输入账号" }]}>
          <Input size="large" autoFocus autoComplete="username" />
        </Form.Item>
        <Form.Item name="password" label="密码" rules={[{ required: true, message: "请输入密码" }]}>
          <Input.Password size="large" autoComplete="current-password" />
        </Form.Item>
        <Button type="primary" htmlType="submit" size="large" block loading={busy}>
          登录
        </Button>
      </Form>
      <div className={styles.help}>
        <span className="ops-muted">忘记密码请找其他运营管理员重置</span>
      </div>
    </AuthCard>
  );
}
