"use client";

import { Spin } from "antd";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { ApiError } from "@/lib/api";
import { landingPath, useMe } from "@/lib/session";

export default function Index() {
  const router = useRouter();
  const { data, error } = useMe();
  useEffect(() => {
    if (error instanceof ApiError && error.status === 401) router.replace("/login");
    else if (data?.must_change_password) router.replace("/change-password");
    else if (data) router.replace(landingPath(data));
  }, [data, error, router]);
  return <Spin style={{ display: "block", marginTop: 120 }} />;
}
