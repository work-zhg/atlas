"use client";

/**
 * 须改密时的独立地址：各应用经网关拿到「须改密」跳来，或登录页发现已登录但须改密。
 * 界面与登录页第二步相同；这里没有刚输入的临时密码，所以多一栏「当前密码」。
 */
import { useQueryClient } from "@tanstack/react-query";
import { Spin } from "antd";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { AuthCard, SetPasswordForm } from "@/components/auth/AuthCard";
import { ApiError, api } from "@/lib/api";
import { goNext, useMe } from "@/lib/session";

export default function ChangePasswordPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const { data: me, error } = useMe();
  useEffect(() => {
    if (error instanceof ApiError && error.status === 401) router.replace("/login");
  }, [error, router]);

  if (!me) return <Spin style={{ display: "block", marginTop: 120 }} />;
  return (
    <AuthCard>
      <SetPasswordForm
        me={me}
        submitText="设置并进入"
        onDone={() => {
          if (!goNext()) router.replace("/");
        }}
        onSwitchAccount={async () => {
          await api.post("/auth/logout").catch(() => undefined);
          qc.clear();
          router.replace("/login");
        }}
      />
    </AuthCard>
  );
}
