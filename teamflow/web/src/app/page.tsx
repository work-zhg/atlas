"use client";

import { Spin } from "antd";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { BackendDown, isAuthRedirect } from "@/components/BackendDown";
import { ApiError, ucLoginUrl } from "@/lib/api";
import { landingPath, useMe } from "@/lib/session";

export default function Index() {
  const router = useRouter();
  const { data, error, refetch, isFetching } = useMe();
  useEffect(() => {
    if (error instanceof ApiError && error.status === 401) window.location.assign(ucLoginUrl());
    else if (data) router.replace(landingPath(data));
  }, [data, error, router]);
  if (error && !isAuthRedirect(error)) return <BackendDown error={error} onRetry={() => void refetch()} retrying={isFetching} />;
  return <Spin style={{ display: "block", marginTop: 120 }} />;
}
