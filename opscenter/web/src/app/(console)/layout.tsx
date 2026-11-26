"use client";

/**
 * 运营控制台：登录统一在用户中心（c4-model/opscenter/design-opscenter.html）。
 * 没登录（401，刷新也不行）→ 整页跳统一登录页；不是系统租户的人（403 NOT_OPERATOR）→ 说明怎么开通。
 * 骨架见 components/shell/AppShell.tsx。
 */
import { Result, Spin } from "antd";
import { useEffect, type ReactNode } from "react";

import { AppShell } from "@/components/shell/AppShell";
import { ApiError, loginUrl, logout } from "@/lib/api";
import { useMe } from "@/lib/session";

export default function ConsoleLayout({ children }: { children: ReactNode }) {
  const { data: me, error } = useMe();
  const unauth = error instanceof ApiError && error.status === 401;

  useEffect(() => {
    if (unauth) window.location.assign(loginUrl());
  }, [unauth]);

  if (error instanceof ApiError && error.code === "NOT_OPERATOR") {
    return (
      <Result
        status="403"
        title="你还没有开通运营中心"
        subTitle="运营中心只给系统租户的人用：请系统租户的管理员在用户中心给你开账号、授运营模块。"
        extra={<a onClick={() => void logout()}>换个账号登录</a>}
      />
    );
  }
  if (!me) return <Spin style={{ display: "block", marginTop: 120 }} />;
  return <AppShell me={me}>{children}</AppShell>;
}
