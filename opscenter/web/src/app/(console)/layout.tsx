"use client";

/** 运营控制台：没登录去登录页，临时密码先去改密码；骨架见 components/shell/AppShell.tsx。 */
import { Spin } from "antd";
import { useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { AppShell } from "@/components/shell/AppShell";
import { ApiError } from "@/lib/api";
import { useMe } from "@/lib/session";

export default function ConsoleLayout({ children }: { children: ReactNode }) {
  const router = useRouter();
  const { data: me, error } = useMe();

  useEffect(() => {
    if (error instanceof ApiError && error.status === 401) router.replace("/login");
    else if (error instanceof ApiError && error.code === "PASSWORD_CHANGE_REQUIRED") router.replace("/password");
    else if (me?.must_change_password) router.replace("/password");
  }, [me, error, router]);

  if (!me || me.must_change_password) return <Spin style={{ display: "block", marginTop: 120 }} />;
  return <AppShell me={me}>{children}</AppShell>;
}
