"use client";

/**
 * 系统设置 › 定时任务：TeamFlow 的实例任务（仓库协作者对账、扫描待处理节点，atlas_teamflow/jobs.py）
 * 在回应请求的那个实例上的运行情况。要 settings:manage（api/jobs.py）；面板是共享组件 @atlas/jobs-ui。
 * 独立 Worker 进程里的扫描不在这里（Worker 没有 HTTP 接口）。
 */
import { JobsPanel } from "@atlas/jobs-ui";

import { PageHead } from "@/components/common";
import { BASE_PATH } from "@/lib/api";

const ENDPOINT = `${BASE_PATH}/api/v1/jobs`;

export default function JobsPage() {
  return (
    <>
      <PageHead title="定时任务" sub="系统设置 · 后台定期执行的任务（系统机制，只能查看）" />
      <JobsPanel endpoint={ENDPOINT} />
    </>
  );
}
