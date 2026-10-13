"use client";

import { useParams, useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { FilePreviewPage } from "@/features/files/FilePreviewPage";

/**
 * /files/<threadId>?path=<工作区相对路径> —— 会话文件预览，从文件面板新标签打开。
 *
 * path 放 query 而不是路径段：工作区路径本身含 /，放进路径段要整体转义，
 * 地址栏里就不可读了。
 */
function Preview() {
  const { threadId } = useParams<{ threadId: string }>();
  const path = useSearchParams().get("path") ?? "";
  return <FilePreviewPage threadId={threadId} path={path} />;
}

export default function FilePreviewRoute() {
  // useSearchParams 需要 Suspense 边界（Next 15 预渲染要求）
  return (
    <Suspense>
      <Preview />
    </Suspense>
  );
}
