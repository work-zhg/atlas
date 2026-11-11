"use client";

/**
 * 定时任务的管理面板（原型 c4-model/infra-platform/ui-jobs.html）：列表 → 点一行打开详情抽屉。
 *
 * ★ 这一版只读、只有实例任务（系统机制）：定义写在代码里，每个实例各跑一份。新建、编辑、暂停、
 *   立即触发、执行历史等集群任务实现后再加。
 * ★ 指标是回应请求的那个实例的，顶部写明是哪个实例；多副本时每次刷新可能换一个实例。
 *
 * 页面标题、导航由宿主自己画；这里只管面板本身。
 */
import { Alert, Button, Flex, Input, Table, type TableColumnType, Typography } from "antd";
import { useMemo, useState } from "react";

import { type InstanceJobOut, type JobsClient, REFRESH_MS, useJobs } from "./api";
import { JobDetailDrawer } from "./JobDetailDrawer";
import { JobName, JobStatus, useMono } from "./JobStatus";
import { schedule, upcoming, useNow } from "./format";

export type JobsPanelProps = JobsClient;

export function JobsPanel({ endpoint, headers }: JobsPanelProps) {
  const client = useMemo(() => ({ endpoint, headers }), [endpoint, headers]);
  const q = useJobs(client);
  const now = useNow();
  const mono = useMono();
  const [keyword, setKeyword] = useState("");
  const [detail, setDetail] = useState<{ type: string; open: boolean } | null>(null);

  const jobs = useMemo(() => q.data?.jobs ?? [], [q.data]);
  const rows = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    return kw ? jobs.filter((j) => `${j.type}\n${j.description}`.toLowerCase().includes(kw)) : jobs;
  }, [jobs, keyword]);

  const columns: TableColumnType<InstanceJobOut>[] = [
    {
      title: "名称",
      width: "32%",
      render: (_, j) => (
        <div>
          <JobName job={j} />
          {j.description && (
            <div style={{ ...mono, fontSize: 12 }}>
              <Typography.Text type="secondary">{j.type}</Typography.Text>
            </div>
          )}
        </div>
      ),
    },
    {
      title: "触发规则",
      width: "22%",
      render: (_, j) => (
        <div>
          {schedule(j)}
          {j.next_fire_at && (
            <div style={{ fontSize: 12 }}>
              <Typography.Text type="secondary">下一轮 {upcoming(j.next_fire_at, now)}</Typography.Text>
            </div>
          )}
        </div>
      ),
    },
    { title: "运行份数", width: "12%", render: () => "每个实例" },
    { title: "状态", width: "18%", render: (_, j) => <JobStatus job={j} now={now} /> },
    {
      title: "操作",
      render: (_, j) => (
        <Button type="text" size="small" onClick={() => setDetail({ type: j.type, open: true })}>
          详情
        </Button>
      ),
    },
  ];

  return (
    <div>
      {q.data && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message={
            <span>
              下面是实例 <span style={mono}>{q.data.instance}</span> 上的运行情况
            </span>
          }
          description="这些都是系统机制：定义写在代码里，每个实例各跑一份，只能查看；进程重启后计数清零。多副本部署时，每次刷新可能由不同的实例回应。"
        />
      )}

      <Flex align="center" gap={12} wrap style={{ marginBottom: 12 }}>
        <Input
          allowClear
          value={keyword}
          placeholder="搜索名称或类型"
          style={{ width: 260 }}
          onChange={(e) => setKeyword(e.target.value)}
        />
        <span style={{ flex: 1 }} />
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          每 {REFRESH_MS / 1000} 秒自动刷新
        </Typography.Text>
        <Button loading={q.isFetching} onClick={() => void q.refetch()}>
          刷新
        </Button>
      </Flex>

      {q.error ? (
        <Alert type="error" showIcon message="读取定时任务失败" description={q.error.message} />
      ) : (
        <Table<InstanceJobOut>
          rowKey="type"
          size="middle"
          loading={q.isLoading}
          pagination={false}
          dataSource={rows}
          columns={columns}
          locale={{ emptyText: jobs.length === 0 ? "这个实例上没有定时任务" : "没有符合条件的任务" }}
          onRow={(j) => ({ onClick: () => setDetail({ type: j.type, open: true }), style: { cursor: "pointer" } })}
        />
      )}

      {detail && (
        <JobDetailDrawer
          job={jobs.find((j) => j.type === detail.type)}
          instance={q.data?.instance ?? ""}
          open={detail.open}
          now={now}
          onClose={() => setDetail((d) => d && { ...d, open: false })}
        />
      )}
    </div>
  );
}
