"use client";

/**
 * 定时任务（系统租户）：TeamFlow 的实例任务（投递器兜底扫描、通知）在回应请求的那个实例上的运行情况。
 * 面板是共享组件 @atlas/jobs-ui；接口只给系统租户的人（api/app.py）。
 */
import { refreshSession } from "@atlas/chat-ui/src/lib/session";
import { JobsPanel } from "@atlas/jobs-ui";

import { Page, PageHead } from "@/components/ui";
import { API_BASE } from "@/lib/api";

const ENDPOINT = `${API_BASE}/api/v1/jobs`;

export default function JobsPage() {
  return (
    <Page>
      <PageHead title="定时任务" sub="后台定期执行的任务（系统机制，只能查看）" />
      <JobsPanel endpoint={ENDPOINT} refresh={refreshSession} />
    </Page>
  );
}
