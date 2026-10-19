"use client";

/**
 * 旧的 TeamFlow 登录页：登录已统一到用户中心（doc/identity-propagation-design.html §4）。
 * 保留这个地址只为旧书签：整页跳到 /uc/login，登录后回到 TeamFlow 首页。
 */
import { Spin } from "antd";
import { useEffect } from "react";

import { BASE_PATH, ucLoginUrl } from "@/lib/api";

export default function LoginPage() {
  useEffect(() => {
    window.location.replace(ucLoginUrl(`${BASE_PATH}/`));
  }, []);
  return <Spin style={{ display: "block", marginTop: 120 }} />;
}
