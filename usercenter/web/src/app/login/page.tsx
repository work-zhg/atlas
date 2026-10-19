"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Form, Input } from "antd";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { api, errorText } from "@/lib/api";
import { goNext, useMe, withNext } from "@/lib/session";

export default function LoginPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const [err, setErr] = useState<string>();
  const [loading, setLoading] = useState(false);
  // 只有一个租户（单租户部署）时不显示企业代码
  const needTenant = useQuery({
    queryKey: ["tenant-required"],
    queryFn: () => api.get<{ required: boolean }>("/auth/tenant-required"),
  });
  const lastTenant = typeof window === "undefined" ? undefined : localStorage.getItem("uc_tenant") ?? undefined;
  // 已登录但须先改密（各应用经网关拿到 401 跳来的）：直接去改密，改完回原页面
  const me = useMe();
  useEffect(() => {
    if (me.data?.must_change_password) router.replace(withNext("/change-password"));
  }, [me.data, router]);

  async function submit(values: { tenant?: string; account: string; password: string }) {
    setLoading(true);
    setErr(undefined);
    try {
      const r = await api.post<{ must_change_password: boolean }>("/auth/login", values);
      if (values.tenant) localStorage.setItem("uc_tenant", values.tenant.trim().toLowerCase());
      await qc.invalidateQueries();
      // 从 Atlas / TeamFlow 跳来登录的：登录后回原页面（须改密的先改密，next 跟着走）
      if (r.must_change_password) router.replace(withNext("/change-password"));
      else if (!goNext()) router.replace("/");
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
          <Form layout="vertical" onFinish={submit} requiredMark={false} initialValues={{ tenant: lastTenant }}>
            {needTenant.data?.required && (
              <Form.Item name="tenant" label="企业代码" rules={[{ required: true, message: "请输入企业代码" }]}>
                <Input autoComplete="organization" autoFocus={!lastTenant} placeholder="由管理员提供" />
              </Form.Item>
            )}
            <Form.Item name="account" label="账号" rules={[{ required: true, message: "请输入账号" }]}>
              <Input autoComplete="username" autoFocus={!needTenant.data?.required || !!lastTenant} />
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
