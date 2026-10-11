"use client";

/**
 * 登录守卫：GET /v1/me 401 → 去登录页（带回跳地址）；dev 模式后端直接返回 dev 身份，不需要登录。
 * 后端不可达时给出明确的错误和重试，而不是一直转圈。
 */
import { Button, Result, Spin } from "antd";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { ApiError } from "@/lib/http";
import { useMe } from "@/lib/session";

/** 不需要登录的页面：登录页；文件预览页靠预览令牌，不走会话 */
const PUBLIC = ["/login"];

export function AuthGate({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const open = PUBLIC.some((p) => pathname === p || pathname.startsWith(`${p}/`));
  const { data, error, isFetching, refetch } = useMe();
  const unauth = error instanceof ApiError && error.status === 401;

  useEffect(() => {
    if (!open && unauth) router.replace(`/login?next=${encodeURIComponent(pathname)}`);
  }, [open, unauth, pathname, router]);

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
