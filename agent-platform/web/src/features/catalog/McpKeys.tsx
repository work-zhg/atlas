"use client";

/**
 * MCP 管理 · API Key（MCP 方案设计 §5.5、§9.1）。
 *
 *   一个主体（人或数字员工）最多一把，只表示「我是谁」，不带权限 —— 能用什么看用户中心的授权
 *   发放时可以同时选服务：写的是持有人的服务授权，不是「Key 的权限」
 *   重置 / 停用立即生效（网关每个请求查库）；启用时重新生成，不恢复旧值；不提供删除
 *   原文谁都看不到（Q3）：Key 只给 Atlas 自己用，这里只显示掩码
 */
import { useQuery } from "@tanstack/react-query";
import { Alert, App as AntdApp, Button, Modal, Popconfirm, Select, Space, Table, Tag } from "antd";
import { useState } from "react";

import { type GwKey, type GwService, useGwKeyActions, useGwKeys, useGwMissingKeys } from "@/api/mcp-gateway";
import { directory } from "@/features/knowledge/api";
import { relativeTime } from "@/lib/format";
import styles from "./catalog.module.css";

export function McpKeys({ services }: { services: GwService[] }) {
  const { message } = AntdApp.useApp();
  const keysQ = useGwKeys(true);
  const missingQ = useGwMissingKeys(true);
  const { issue, act } = useGwKeyActions();
  const [issuing, setIssuing] = useState(false);
  const [bulk, setBulk] = useState(false);

  const issueAll = async () => {
    const missing = missingQ.data ?? [];
    setBulk(true);
    let ok = 0;
    for (const m of missing) {
      try {
        await issue.mutateAsync({ subject_uid: m.subject_uid, services: [] });
        ok += 1;
      } catch (e) {
        message.error(`${m.name}：${(e as Error).message}`);
      }
    }
    setBulk(false);
    message.success(`已发放 ${ok} 把`);
  };

  const run = (id: string, action: "reset" | "disable" | "enable", done: string) =>
    act.mutate({ id, action }, { onSuccess: () => message.success(done), onError: (e) => message.error(e.message) });

  return (
    <div>
      <Space style={{ marginBottom: 12 }}>
        <Button type="primary" onClick={() => setIssuing(true)}>
          发放 Key
        </Button>
      </Space>

      {(missingQ.data ?? []).length > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={`${missingQ.data!.length} 个人有授权但没有 Key（调不了 MCP 服务）`}
          description={
            <div>
              <div className={styles.muted} style={{ marginBottom: 6 }}>
                {missingQ.data!.map((m) => `${m.name}（${m.services.join("、")}）`).join("；")}
              </div>
              <Button size="small" loading={bulk} onClick={() => void issueAll()}>
                全部发放
              </Button>
            </div>
          }
        />
      )}

      <Table<GwKey>
        size="middle"
        rowKey="id"
        loading={keysQ.isPending}
        dataSource={keysQ.data ?? []}
        pagination={{ pageSize: 50, hideOnSinglePage: true }}
        locale={{ emptyText: "还没有发放过 Key" }}
        columns={[
          {
            title: "持有人",
            render: (_, k) => (
              <span>
                {k.subject_type === "digital" && <Tag color="purple">数字员工</Tag>}
                {k.subject_name || k.subject_uid}
              </span>
            ),
          },
          {
            title: "Key",
            width: 190,
            render: (_, k) => <span style={{ fontFamily: "var(--mono)" }}>{k.masked}</span>,
          },
          {
            title: "状态",
            width: 90,
            render: (_, k) => (k.status === "active" ? <Tag color="success">可用</Tag> : <Tag>已停用</Tag>),
          },
          { title: "最近生成", width: 110, render: (_, k) => <span className={styles.muted}>{relativeTime(k.rotated_at)}</span> },
          {
            title: "",
            width: 200,
            render: (_, k) => (
              <Space size={0}>
                <Popconfirm title="重置后旧 Key 立即失效" onConfirm={() => run(k.id, "reset", "已重置")}>
                  <Button size="small" type="link">
                    重置
                  </Button>
                </Popconfirm>
                {k.status === "active" ? (
                  <Popconfirm title="停用后立即调不了 MCP 服务" onConfirm={() => run(k.id, "disable", "已停用")}>
                    <Button size="small" type="link" danger>
                      停用
                    </Button>
                  </Popconfirm>
                ) : (
                  <Button size="small" type="link" onClick={() => run(k.id, "enable", "已启用（重新生成了 Key）")}>
                    启用
                  </Button>
                )}
              </Space>
            ),
          },
        ]}
      />
      <p className={styles.muted} style={{ marginTop: 8 }}>
        Key 只表示「我是谁」：能用哪些服务看授权。原文只给 Atlas 运行时使用，这里谁都看不到。
      </p>

      <IssueModal open={issuing} onClose={() => setIssuing(false)} services={services} />
    </div>
  );
}

function IssueModal({ open, onClose, services }: { open: boolean; onClose: () => void; services: GwService[] }) {
  const { message } = AntdApp.useApp();
  const { issue } = useGwKeyActions();
  const [q, setQ] = useState("");
  const users = useQuery({
    queryKey: ["directory", "users", q, "digital"],
    queryFn: () => directory.users(q, true),
    staleTime: 30_000,
    enabled: open,
  });
  const [subject, setSubject] = useState<string>();
  const [picked, setPicked] = useState<string[]>([]);
  const people = (users.data ?? []).filter((u) => u.user_type !== "digital");
  const agents = (users.data ?? []).filter((u) => u.user_type === "digital");
  const isAgent = agents.some((a) => a.id === subject);

  const close = () => {
    setSubject(undefined);
    setPicked([]);
    issue.reset();
    onClose();
  };

  return (
    <Modal
      title="发放 API Key"
      open={open}
      onCancel={close}
      okText="发放"
      okButtonProps={{ disabled: !subject, loading: issue.isPending }}
      onOk={() =>
        subject &&
        issue.mutate(
          { subject_uid: subject, services: picked },
          {
            onSuccess: () => {
              message.success("已发放");
              close();
            },
          },
        )
      }
    >
      <div style={{ display: "grid", gap: 10 }}>
        <Select
          showSearch
          filterOption={false}
          onSearch={setQ}
          value={subject}
          onChange={setSubject}
          placeholder="持有人：人员或数字员工（输入名字或账号搜索）"
          options={[
            { label: "人员", options: people.map((u) => ({ value: u.id, label: `${u.name}（${u.account}）` })) },
            { label: "🤖 数字员工", options: agents.map((u) => ({ value: u.id, label: u.name })) },
          ]}
        />
        <Select
          mode="multiple"
          value={picked}
          onChange={setPicked}
          placeholder="同时授这些服务的「使用」（可选）"
          options={services.filter((s) => s.enabled).map((s) => ({ value: s.name, label: s.display_name || s.name }))}
        />
        {isAgent && picked.length > 0 && (
          <Alert type="warning" showIcon message="授给数字员工 = 授给这个 Agent 的所有使用者" />
        )}
        <p className={styles.muted} style={{ margin: 0 }}>
          勾选的服务写的是持有人的服务授权（用户中心），重置 Key 不会丢；你要是这些服务的所有者才能授。
        </p>
        {issue.error && <Alert type="error" showIcon message="发放失败" description={issue.error.message} />}
      </div>
    </Modal>
  );
}
