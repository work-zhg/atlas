"use client";

import { useQuery } from "@tanstack/react-query";
import { List, Space, Tag } from "antd";
import Link from "next/link";

import { EmptyBox, PageHead } from "@/components/common";
import { api } from "@/lib/api";
import { dt } from "@/lib/format";
import type { TodoItem } from "@/lib/types";

const KIND: Record<TodoItem["kind"], [string, string]> = {
  work: ["人机协同", "blue"],
  exit_review: ["准出评审", "orange"],
  admit_review: ["准入评审", "purple"],
};

export default function TodoPage() {
  const { data = [], isLoading } = useQuery({
    queryKey: ["todo"],
    queryFn: () => api.get<TodoItem[]>("/todo"),
    refetchInterval: 15_000,
  });
  return (
    <>
      <PageHead title="待我处理" sub="跨项目汇总：需要我协同的节点、需要我投票的评审" />
      {!isLoading && !data.length ? (
        <EmptyBox text="没有待处理的事项 🎉" />
      ) : (
        <List
          bordered
          loading={isLoading}
          style={{ background: "#fff" }}
          dataSource={data}
          renderItem={(t) => (
            <List.Item>
              <Link href={`/processes/${t.process_id}?node=${t.node_id}`} style={{ color: "inherit", flex: 1 }}>
                <Space>
                  <Tag color={KIND[t.kind][1]}>{KIND[t.kind][0]}</Tag>
                  <span className="tf-muted tf-mono">#{t.process_no}</span>
                  <b>{t.process_title}</b>
                  <span>· {t.node_name}</span>
                </Space>
              </Link>
              <span className="tf-muted" style={{ fontSize: 12.5 }}>
                {dt(t.since)}
              </span>
            </List.Item>
          )}
        />
      )}
    </>
  );
}
