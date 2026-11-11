"use client";

import { Suspense } from "react";

import { HomePage } from "@/features/home/HomePage";

/** 智能平台首页：输入框，发出第一句话即新建对话 */
export default function Page() {
  return (
    <Suspense>
      <HomePage />
    </Suspense>
  );
}
