"use client";

/**
 * 详情抽屉：系统机制只显示本实例的运行情况（原型 ui-jobs.html 的「系统机制」详情）。
 * 数据跟着列表每 5 秒刷新，不单独请求。
 */
import { Alert, Button, Descriptions, Drawer, Typography } from "antd";

import type { InstanceJobOut } from "./api";
import { JobName, JobStatus, SYSTEM_HINT, useMono } from "./JobStatus";
import { clock, duration, relative, schedule, upcoming } from "./format";

export interface JobDetailDrawerProps {
  job: InstanceJobOut | undefined;
  instance: string;
  open: boolean;
  now: number;
  onClose: () => void;
}

export function JobDetailDrawer({ job, instance, open, now, onClose }: JobDetailDrawerProps) {
  const mono = useMono();
  const at = (iso: string | null) => (iso ? `${clock(iso)}（${relative(iso, now)}）` : "—");

  return (
    <Drawer
      open={open}
      width={560}
      onClose={onClose}
      title={
        job && (
          <div>
            <JobName job={job} />
            <div style={{ marginTop: 4, fontWeight: "normal", fontSize: 12 }}>
              <Typography.Text type="secondary">
                <span style={mono}>{job.type}</span> · 每个实例 · {schedule(job)}
              </Typography.Text>
            </div>
          </div>
        )
      }
      footer={<Button onClick={onClose}>关闭</Button>}
    >
      {job && (
        <>
          {job.consecutive_failures > 0 && (
            <Alert
              type="error"
              showIcon
              style={{ marginBottom: 12 }}
              message={`最近 ${job.consecutive_failures} 轮连续失败`}
              description={
                job.last_error && (
                  <div style={{ ...mono, fontSize: 12, whiteSpace: "pre-wrap", wordBreak: "break-all" }}>{job.last_error}</div>
                )
              }
            />
          )}
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message={SYSTEM_HINT}
            description="每个实例各跑一份，不写执行记录；下面是本实例的情况，进程重启后计数清零。"
          />
          <Descriptions
            column={1}
            size="small"
            bordered
            items={[
              { key: "instance", label: "实例", children: <span style={mono}>{instance}</span> },
              { key: "status", label: "状态", children: <JobStatus job={job} now={now} /> },
              {
                key: "next",
                label: "下一轮",
                children: job.next_fire_at
                  ? `${clock(job.next_fire_at)}（${upcoming(job.next_fire_at, now)}）`
                  : job.running
                    ? "这一轮跑完后再排"
                    : "—",
              },
              { key: "runs", label: "已运行", children: `${job.runs} 轮，其中失败 ${job.failures} 轮` },
              {
                key: "result",
                label: "上一轮结果",
                children: !job.last_finished_at ? (
                  "还没跑完过一轮"
                ) : job.consecutive_failures > 0 ? (
                  <Typography.Text type="danger">失败</Typography.Text>
                ) : (
                  <Typography.Text type="success">成功</Typography.Text>
                ),
              },
              { key: "started", label: "最近开始", children: at(job.last_started_at) },
              { key: "finished", label: "最近结束", children: at(job.last_finished_at) },
              {
                key: "duration",
                label: "最近耗时",
                children: job.last_duration_s === null ? "—" : duration(job.last_duration_s),
              },
            ]}
          />
        </>
      )}
    </Drawer>
  );
}
