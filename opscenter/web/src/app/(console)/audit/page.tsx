"use client";

/** 运营审计：谁在什么时候做了什么。只追加，不记任何密码、临时密码、密钥。 */
import { useInfiniteQuery } from "@tanstack/react-query";
import { Button, Input, Space, Table, Tag, Typography } from "antd";
import { useState } from "react";

import { ErrorBox, PageHead } from "@/components/common";
import { api } from "@/lib/api";
import { dateTime } from "@/lib/format";
import type { AuditEntry } from "@/lib/types";

const PAGE = 50;

export default function AuditPage() {
  const [action, setAction] = useState("");
  const q = useInfiniteQuery({
    queryKey: ["audit", action],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => api.get<AuditEntry[]>("/audit", { action, before: pageParam, limit: PAGE }),
    getNextPageParam: (last) => (last.length === PAGE ? last[last.length - 1]?.at : undefined),
  });
  const rows = q.data?.pages.flat() ?? [];
  return (
    <>
      <PageHead title="运营审计" sub="每个写操作一条；跨系统的操作（开通、停用、关闭）做完后补记结果" />
      <Space style={{ marginBottom: 12 }}>
        <Input.Search placeholder="按操作筛选，如 tenant. / system. / login." allowClear onSearch={setAction} style={{ width: 320 }} />
      </Space>
      <ErrorBox error={q.error} />
      <Table<AuditEntry>
        rowKey="id"
        size="small"
        loading={q.isLoading}
        dataSource={rows}
        pagination={false}
        expandable={{
          rowExpandable: (r) => !!r.detail,
          expandedRowRender: (r) => (
            <Typography.Paragraph style={{ fontFamily: "monospace", fontSize: 12, margin: 0, whiteSpace: "pre-wrap" }}>
              {JSON.stringify(r.detail, null, 2)}
            </Typography.Paragraph>
          ),
        }}
        columns={[
          { title: "时间", dataIndex: "at", render: dateTime, width: 180 },
          { title: "运营人员", dataIndex: "operator", render: (v: string | null) => v ?? "—" },
          { title: "操作", dataIndex: "action", render: (v: string) => <Typography.Text code>{v}</Typography.Text> },
          { title: "对象", dataIndex: "target" },
          {
            title: "结果",
            dataIndex: "result",
            render: (v: string | null) =>
              v === "OK" ? <Tag color="green">成功</Tag> : v === "FAILED" ? <Tag color="red">失败</Tag> : <Tag>进行中</Tag>,
          },
        ]}
      />
      {q.hasNextPage && (
        <Button style={{ marginTop: 12 }} loading={q.isFetchingNextPage} onClick={() => void q.fetchNextPage()}>
          更早的
        </Button>
      )}
    </>
  );
}
