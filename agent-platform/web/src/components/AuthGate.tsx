"use client";

/**
 * 登录守卫：GET /v1/me 401 → 整页跳到用户中心登录页（带回跳地址，doc/identity-propagation-design.html §4）；
 * dev 模式后端直接返回 dev 身份，不需要登录。后端不可达时给出明确的错误和重试，而不是一直转圈。
 */
import { Button, Result, Spin } from "antd";
import { usePathname } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { ApiError } from "@/lib/http";
import { ucLoginUrl, useMe } from "@/lib/session";

/** 不需要登录的页面：旧登录页（只做跳转） */
const PUBLIC = ["/login"];

export function AuthGate({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const open = PUBLIC.some((p) => pathname === p || pathname.startsWith(`${p}/`));
  const { data, error, isFetching, refetch } = useMe();
  const unauth = error instanceof ApiError && error.status === 401;

  useEffect(() => {
    if (!open && unauth) window.location.assign(ucLoginUrl());
  }, [open, unauth, pathname]);

  if (open) return <>{children}</>;
  if (data) return <>{children}</>;
  if (error && !unauth)
    return (
      <Result
        status="warning"
        title="暂时连不上 Atlas 服务"
        subTitle={(error as Error).message}
        extra={
          <Button type="primary" loading={isFetching} onClick={() => void refetch()}>
            重试
          </Button>
        }
        style={{ margin: "80px auto" }}
      />
    );
  return <Spin style={{ display: "block", margin: "120px auto" }} />;
}
