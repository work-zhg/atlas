"use client";

/**
 * 旧地址（权限管理）：页签拆成了「权限」下的二级菜单，「数据授权」挪到个人中心。这里只为旧书签跳转。
 */
import { Spin } from "antd";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect } from "react";

const TAB_TARGET: Record<string, string> = { data: "/me/data", pos: "/perm/positions", role: "/perm/grants" };

function Redirect() {
  const router = useRouter();
  const params = useSearchParams();
  useEffect(() => {
    const subject = params.get("subject");
    const target = subject ? `/perm/grants?subject=${encodeURIComponent(subject)}` : (TAB_TARGET[params.get("tab") ?? ""] ?? "/perm/grants");
    router.replace(target);
  }, [params, router]);
  return <Spin style={{ display: "block", marginTop: 120 }} />;
}

export default function OldGrantsPage() {
  return (
    <Suspense>
      <Redirect />
    </Suspense>
  );
}
