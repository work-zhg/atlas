"use client";

/**
 * 旧的 Atlas 登录页：登录已统一到用户中心（doc/identity-propagation-design.html §4）。
 * 保留这个地址只为旧书签：整页跳到 /uc/login，登录后回到 next（缺省对话页）。
 */
import { Spin } from "antd";
import { useEffect } from "react";

import { BASE_PATH } from "@/lib/http";
import { ucLoginUrl } from "@/lib/session";

export default function LoginPage() {
  useEffect(() => {
    const next = new URLSearchParams(window.location.search).get("next");
    const target = next && next.startsWith("/") && !next.startsWith("//") ? next : "/chat";
    window.location.replace(ucLoginUrl(`${BASE_PATH}${target}`));
  }, []);
  return <Spin style={{ display: "block", margin: "120px auto" }} />;
}
