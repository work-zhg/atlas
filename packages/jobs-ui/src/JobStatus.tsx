"use client";

/**
 * 名称与状态（列表和详情共用）。
 *
 * 状态只有「启用 / 运行中」（原型的「已暂停」要等集群任务）；连续失败单独一行红字 —— 系统机制一直
 * 失败时列表上要看得出来，不能只显示一个绿点。
 */
import { Badge, Tooltip, Typography, theme } from "antd";

import type { InstanceJobOut } from "./api";
import { duration, relative } from "./format";

export function useMono() {
  const { token } = theme.useToken();
  return { fontFamily: token.fontFamilyCode };
}

export const SYSTEM_HINT = "系统机制：系统运行必需，定义写在代码里，只能查看";

export function JobName({ job }: { job: InstanceJobOut }) {
  return (
    <span>
      <Tooltip title={SYSTEM_HINT}>
        <span style={{ marginRight: 6 }}>🔒</span>
      </Tooltip>
      {job.description || job.type}
    </span>
  );
}

export function JobStatus({ job, now }: { job: InstanceJobOut; now: number }) {
  const sub = (text: string) => (
    <div style={{ fontSize: 12 }}>
      <Typography.Text type="secondary">{text}</Typography.Text>
    </div>
  );
  return (
    <div>
      {job.running ? <Badge status="processing" text="运行中" /> : <Badge status="success" text="启用" />}
      {job.running && job.last_started_at
        ? sub(`已跑 ${duration(Math.max(0, now - Date.parse(job.last_started_at)) / 1000)}`)
        : job.last_finished_at
          ? sub(`上一轮 ${relative(job.last_finished_at, now)}`)
          : sub("还没跑过")}
      {job.consecutive_failures > 0 && (
        <div style={{ fontSize: 12 }}>
          <Typography.Text type="danger">连续失败 {job.consecutive_failures} 次</Typography.Text>
        </div>
      )}
    </div>
  );
}
