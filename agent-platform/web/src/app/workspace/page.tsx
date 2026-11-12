"use client";

import { Suspense } from "react";

import { WorkspacePage } from "@/features/workspace/WorkspacePage";

/** /workspace?dir=<目录>&file=<预览的文件> —— 我的工作空间（路径都相对工作空间的根） */
export default function Page() {
  // useSearchParams 需要 Suspense 边界（Next 15 预渲染要求）
  return (
    <Suspense>
      <WorkspacePage />
    </Suspense>
  );
}
