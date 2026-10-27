"use client";

/**
 * 修订历史（doc/memory-design.html §3.4、§4.7）：每次写入删除了哪些、新增了哪一条。
 *
 * 撤销一次写入 = 反向执行：撤销新增是删除新条目；撤销取代是恢复旧条目、删除新条目。
 * ★ 只有写入能撤销，且只在它的结果仍是现状时（之后又被改过就要先看清现状，后端返回 409）。
 */
import { Alert, App, Button, Popconfirm, Skeleton, Table, Tag } from "antd";

import { REVISION_KIND_LABEL, SOURCE_LABEL, memoryApi, useMemoryMutation, useMemoryRevisions } from "@/api/memories";
import type { MemoryRevision } from "@/api/types";

import { localDateTime } from "./time";

function numbers(list: number[]) {
  return list.length ? list.map((n) => `#${n}`).join(" ") : "—";
}

export function RevisionsTab({ onOpenEntry }: { onOpenEntry: (n: number) => void }) {
  const { message } = App.useApp();
  const { data, isPending, error } = useMemoryRevisions(true);
  const undo = useMemoryMutation(memoryApi.undo);

  if (error) return <Alert type="error" showIcon message={(error as Error).message} />;
  if (isPending) return <Skeleton active />;

  return (
    <Table<MemoryRevision>
      rowKey="id"
      size="small"
      dataSource={data?.data ?? []}
      pagination={(data?.data.length ?? 0) > 50 ? { pageSize: 50 } : false}
      columns={[
        { title: "时间", width: 150, render: (_, r) => localDateTime(r.created_at) },
        {
          title: "操作",
          width: 90,
          render: (_, r) => (
            <span>
              {REVISION_KIND_LABEL[r.kind] ?? r.kind}
              {r.undone_by && (
                <Tag style={{ marginLeft: 6 }} color="default">
                  已撤销
                </Tag>
              )}
            </span>
          ),
        },
        { title: "主题", width: 140, render: (_, r) => r.topic ?? "—" },
        {
          title: "删除",
          render: (_, r) =>
            r.removed.length ? (
              <span>
                {r.removed.map((n) => (
                  <Button key={n} type="link" size="small" style={{ padding: "0 2px" }} onClick={() => onOpenEntry(n)}>
                    #{n}
                  </Button>
                ))}
              </span>
            ) : (
              numbers(r.removed)
            ),
        },
        {
          title: "新增 / 恢复",
          render: (_, r) =>
            r.added.length ? (
              <span>
                {r.added.map((n) => (
                  <Button key={n} type="link" size="small" style={{ padding: "0 2px" }} onClick={() => onOpenEntry(n)}>
                    #{n}
                  </Button>
                ))}
              </span>
            ) : (
              numbers(r.added)
            ),
        },
        {
          title: "来源",
          width: 110,
          render: (_, r) => (r.source ? (SOURCE_LABEL[r.source] ?? r.source) : "—"),
        },
        {
          title: "",
          width: 80,
          render: (_, r) =>
            r.kind === "write" && !r.undone_by ? (
              <Popconfirm
                title="撤销这次写入？"
                description="新增的条目会被删除，被它取代的旧条目会恢复。"
                okText="撤销"
                cancelText="取消"
                onConfirm={() =>
                  undo
                    .mutateAsync(r.id)
                    .then(() => message.success("已撤销，下一轮对话生效"))
                    .catch((e: Error) => message.error(e.message))
                }
              >
                <Button size="small" type="text">
                  撤销
                </Button>
              </Popconfirm>
            ) : null,
        },
      ]}
    />
  );
}
