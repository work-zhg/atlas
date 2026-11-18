"use client";

import { Spin } from "antd";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { BackendDown, isAuthRedirect } from "@/components/BackendDown";
import { AppShell } from "@/components/shell/AppShell";
import { accessOf, allowed, landingPath } from "@/components/shell/nav";
import { ApiError } from "@/lib/api";
import { useMe } from "@/lib/session";

/** 管理台外壳：导航见 components/shell/nav.ts（由登录人的菜单编码与操作码决定显示哪些）。 */
export default function ConsoleLayout({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const { data: me, error, refetch, isFetching } = useMe();
  const ok = !!me && allowed(pathname, accessOf(me));

  useEffect(() => {
    if (error instanceof ApiError && error.status === 401) router.replace("/login");
    else if (error instanceof ApiError && error.code === "PASSWORD_CHANGE_REQUIRED") router.replace("/change-password");
    else if (me?.must_change_password) router.replace("/change-password");
    else if (me && !ok) router.replace(landingPath(me));
  }, [me, error, ok, router]);

  if (!me && error && !isAuthRedirect(error))
    return <BackendDown error={error} onRetry={() => void refetch()} retrying={isFetching} />;
  if (!me || me.must_change_password || !ok) return <Spin style={{ display: "block", marginTop: 120 }} />;

  return (
    <AppShell me={me}>
      <div className="uc-page">{children}</div>
    </AppShell>
  );
}
