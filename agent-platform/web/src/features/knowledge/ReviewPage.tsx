"use client";

/**
 * 待审核：所有需要审核、且我能审的知识库里，还在等审核的文档，集中在一处处理。
 *
 * ★ 按库逐个取「待审核」的文档（知识库是几十的量级），不必为它再开一条跨库接口。
 */
import { useQueries } from "@tanstack/react-query";
import { App, Button, Empty, Skeleton, Table } from "antd";
import Link from "next/link";
import { useMemo } from "react";

import { Icon } from "@/components/Icon";
import styles from "@/features/agents/agents.module.css";
import { relativeTime } from "@/lib/format";
import { knowledgeHttp } from "@/lib/http";
import { can, useMe } from "@/lib/session";

import { type Dataset, type KbDocument, kbApi, useDatasets, useKbMutation } from "./api";

interface Row {
  ds: Dataset;
  doc: KbDocument;
}

export function ReviewPage() {
  const { message } = App.useApp();
  const { data: me } = useMe();
  const datasetsQ = useDatasets();
  const reviewable = (datasetsQ.data?.data ?? []).filter(
    (d) => d.review_required && (d.can_admin || can(me, "kb:review")),
  );
  const docsQ = useQueries({
    queries: reviewable.map((d) => ({
      queryKey: ["kb", "documents", d.id, "pending_review"],
      queryFn: () =>
        knowledgeHttp.get<{ data: KbDocument[] }>(`/v1/datasets/${d.id}/documents`, {
          status: "pending_review",
          page: 1,
          limit: 100,
        }),
    })),
  });
  const rows: Row[] = useMemo(
    () => reviewable.flatMap((ds, i) => (docsQ[i]?.data?.data ?? []).map((doc) => ({ ds, doc }))),
    [reviewable, docsQ],
  );
  const act = useKbMutation(async (fn: () => Promise<unknown>) => fn());
  const review = (r: Row, approve: boolean) =>
    act
      .mutateAsync(() => kbApi.review(r.ds.id, [r.doc.id], approve))
      .then(() => message.success(approve ? "已通过，文档可以被查到了" : "已驳回"))
      .catch((e: Error) => message.error(e.message));

  const loading = datasetsQ.isPending || docsQ.some((q) => q.isPending);
  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <div className={styles.titleRow}>
          <h1 className={styles.title}>待审核</h1>
        </div>
      </header>
      <div className={styles.body}>
        {loading ? (
          <Skeleton active />
        ) : rows.length === 0 ? (
          <Empty description="没有等待审核的文档" style={{ marginTop: 60 }} />
        ) : (
          <Table<Row>
            rowKey={(r) => r.doc.id}
            pagination={false}
            dataSource={rows}
            columns={[
              {
                title: "文档",
                render: (_, r) => (
                  <Link href={`/knowledge/${r.ds.id}/documents/${r.doc.id}`}>
                    <Icon name="file" size={14} style={{ verticalAlign: -2, marginRight: 6 }} />
                    {r.doc.name}
                  </Link>
                ),
              },
              { title: "知识库", render: (_, r) => <Link href={`/knowledge/${r.ds.id}`}>{r.ds.name}</Link> },
              { title: "上传时间", render: (_, r) => relativeTime(r.doc.created_at) },
              {
                title: "",
                align: "right",
                render: (_, r) => (
                  <span style={{ display: "inline-flex", gap: 8 }}>
                    <Button size="small" onClick={() => void review(r, false)}>
                      驳回
                    </Button>
                    <Button size="small" type="primary" onClick={() => void review(r, true)}>
                      通过
                    </Button>
                  </span>
                ),
              },
            ]}
          />
        )}
      </div>
    </div>
  );
}
