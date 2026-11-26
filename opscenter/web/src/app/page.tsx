"use client";

/** 首页：去第一个有权限的菜单（每个人能看的模块不一样） */
import { Spin } from "antd";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { homeOf } from "@/components/shell/nav";
import { ApiError, loginUrl } from "@/lib/api";
import { access, useMe, useSystems } from "@/lib/session";

export default function Home() {
  const router = useRouter();
  const { data: me, error } = useMe();
  const { data: systems } = useSystems();

  useEffect(() => {
    if (error instanceof ApiError && error.status === 401) window.location.assign(loginUrl());
    else if (error instanceof ApiError) router.replace("/systems");
    else if (me && systems) router.replace(homeOf({ access: access(me, systems), systems }));
  }, [me, systems, error, router]);
  return <Spin style={{ display: "block", marginTop: 120 }} />;
}
