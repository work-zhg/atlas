"use client";

/**
 * 被要求改密码（临时密码登录后、会话还在时进控制台被拦到这里）。
 * 平时改密码在右上角账号菜单里（components/ChangePassword.tsx）。
 */
import { Spin } from "antd";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { AuthCard, SetPasswordForm } from "@/components/auth/AuthCard";
import { ApiError } from "@/lib/api";
import { useMe } from "@/lib/session";

export default function PasswordPage() {
  const router = useRouter();
  const { data: me, error } = useMe();

  useEffect(() => {
    if (error instanceof ApiError && error.status === 401) router.replace("/login");
  }, [error, router]);

  if (!me) return <Spin style={{ display: "block", marginTop: 120 }} />;
  return (
    <AuthCard>
      <SetPasswordForm me={me} onDone={() => router.replace("/tenants")} />
    </AuthCard>
  );
}
