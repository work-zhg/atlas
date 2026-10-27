"use client";

/**
 * MCP 管理 · 访问日志（MCP 方案设计 §10）：经过 MCP 网关的每个请求，包括 Pod 里的 CLI、
 * Atlas 之外用泄露的 Key 发起的调用，以及被拒的（没 Key、无权、未通过复核……）。
 * MCP 管理员看全部；服务所有者只看自己的服务（要先选服务）。
 */
import { Alert, Select, Space, Table, Tag } from "antd";
import { useState } from "react";

import { GW_OUTCOME, type GwAccessLog, type GwService, useGwAccessLog } from "@/api/mcp-gateway";
import { relativeTime } from "@/lib/format";
import styles from "./catalog.module.css";

export function McpAccessLog({ services, canManage }: { services: GwService[]; canManage: boolean }) {
  const [service, setService] = useState<string | undefined>(canManage ? undefined : services[0]?.name);
  const [outcome, setOutcome] = useState<string>();
  const logQ = useGwAccessLog({ service, outcome }, canManage || !!service);

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Select
          allowClear={canManage}
          style={{ width: 200 }}
          value={service}
          onChange={setService}
          placeholder="全部服务"
          options={services.map((s) => ({ value: s.name, label: s.name }))}
        />
        <Select
          allowClear
          style={{ width: 180 }}
          value={outcome}
          onChange={setOutcome}
          placeholder="全部结果"
          options={Object.entries(GW_OUTCOME).map(([value, label]) => ({ value, label }))}
        />
      </Space>
      {logQ.error && <Alert type="error" showIcon message={logQ.error.message} />}
      <Table<GwAccessLog>
        size="small"
        rowKey="id"
        loading={logQ.isFetching}
        dataSource={logQ.data ?? []}
        pagination={{ pageSize: 50, hideOnSinglePage: true }}
        locale={{ emptyText: "没有记录" }}
        columns={[
          { title: "时间", width: 100, render: (_, r) => <span className={styles.muted}>{relativeTime(r.at)}</span> },
          { title: "服务", width: 110, dataIndex: "service" },
          {
            title: "请求",
            render: (_, r) => (
              <span style={{ fontFamily: "var(--mono)", fontSize: 12 }}>
                {r.rpc_method ?? r.http_method}
                {r.tool ? ` ${r.tool}` : ""}
              </span>
            ),
          },
          {
            title: "权限主体 / 使用者",
            render: (_, r) => (
              <span className={styles.muted} style={{ fontFamily: "var(--mono)", fontSize: 11 }}>
                {r.subject_uid ?? "—"}
                {r.user_uid && r.user_uid !== r.subject_uid ? ` / ${r.user_uid}` : ""}
              </span>
            ),
          },
          {
            title: "结果",
            width: 150,
            render: (_, r) =>
              r.outcome === "ok" ? (
                <Tag color={r.status >= 400 ? "warning" : "success"}>{r.status}</Tag>
              ) : (
                <Tag color="error">{GW_OUTCOME[r.outcome] ?? r.outcome}</Tag>
              ),
          },
          { title: "耗时", width: 80, render: (_, r) => <span className={styles.muted}>{r.duration_ms} ms</span> },
        ]}
      />
    </div>
  );
}
