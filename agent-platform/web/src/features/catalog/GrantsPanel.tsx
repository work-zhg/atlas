"use client";

/**
 * 资源授权面板：谁能用这个资源 = 用户中心的数据权限。MCP 服务、模型共用（和知识库的「共享」是同一种界面）。
 *
 *   使用（READ）    能在 Agent 里选、能经网关调用
 *   所有者（OWNER） 能给别人授权、收回；登记人自动是所有者
 *
 * ★ 授给数字员工 = 授给这个 Agent 的所有使用者（以 Agent 为权限的 Agent 按数字员工判）：选到时强提示。
 */
import { type UseMutationResult, useQuery } from "@tanstack/react-query";
import { Alert, App as AntdApp, Button, Checkbox, Radio, Select, Space, Table, Tag, TreeSelect } from "antd";
import { useMemo, useState } from "react";

import type { GwAcl } from "@/api/mcp-gateway";
import { type DirDept, directory } from "@/features/knowledge/api";
import styles from "./catalog.module.css";

const LEVEL_LABEL: Record<string, string> = { READ: "使用", WRITE: "使用", OWNER: "所有者" };

export interface GrantBody {
  level: "READ" | "OWNER";
  subjects: { type: "user" | "dept"; id: string }[];
  include_sub: boolean;
}

interface DeptNode {
  title: string;
  value: string;
  children: DeptNode[];
}

function deptTree(rows: DirDept[]): DeptNode[] {
  const byParent = new Map<string | null, DirDept[]>();
  for (const d of rows) byParent.set(d.parent_id, [...(byParent.get(d.parent_id) ?? []), d]);
  const build = (parent: string | null): DeptNode[] =>
    (byParent.get(parent) ?? []).map((d) => ({ title: d.name, value: d.id, children: build(d.id) }));
  return build(null);
}

export function GrantsPanel({
  grants,
  add,
  revoke,
  canGrant,
  agentHint,
}: {
  grants: { data?: GwAcl[]; isPending: boolean; error: Error | null };
  add: UseMutationResult<unknown, Error, GrantBody>;
  revoke: UseMutationResult<unknown, Error, string>;
  canGrant: boolean;
  /** 选到数字员工时的说明 */
  agentHint: string;
}) {
  const { message } = AntdApp.useApp();
  const [q, setQ] = useState("");
  const users = useQuery({
    queryKey: ["directory", "users", q, "digital"],
    queryFn: () => directory.users(q, true),
    staleTime: 30_000,
    enabled: canGrant,
  });
  const depts = useQuery({
    queryKey: ["directory", "depts"],
    queryFn: directory.depts,
    staleTime: 60_000,
    enabled: canGrant,
  });
  const tree = useMemo(() => deptTree(depts.data ?? []), [depts.data]);
  const [level, setLevel] = useState<"READ" | "OWNER">("READ");
  const [pickedUsers, setPickedUsers] = useState<string[]>([]);
  const [pickedDepts, setPickedDepts] = useState<string[]>([]);
  const [includeSub, setIncludeSub] = useState(true);
  const people = (users.data ?? []).filter((u) => u.user_type !== "digital");
  const agents = (users.data ?? []).filter((u) => u.user_type === "digital");
  const pickedAgent = pickedUsers.some((id) => agents.some((a) => a.id === id));

  const submit = () => {
    const subjects = [
      ...pickedDepts.map((id) => ({ type: "dept" as const, id })),
      ...pickedUsers.map((id) => ({ type: "user" as const, id })),
    ];
    if (!subjects.length) {
      message.warning("请选择人员、数字员工或部门");
      return;
    }
    add.mutate(
      { level, subjects, include_sub: includeSub },
      {
        onSuccess: () => {
          message.success("已授权。别的副本按判权缓存（约半分钟）生效");
          setPickedUsers([]);
          setPickedDepts([]);
        },
        onError: (e) => message.error(e.message),
      },
    );
  };

  return (
    <>
      {grants.error && <Alert type="error" showIcon message={grants.error.message} />}
      <Table<GwAcl>
        size="small"
        rowKey="id"
        loading={grants.isPending}
        dataSource={grants.data ?? []}
        pagination={false}
        locale={{ emptyText: "还没有授给任何人" }}
        columns={[
          {
            title: "授给",
            render: (_, a) => (
              <span>
                {a.subject.type === "DEPT" ? <Tag>部门</Tag> : a.subject.user_type === "digital" ? <Tag color="purple">数字员工</Tag> : null}
                {a.subject.name}
                {a.subject.type === "DEPT" && a.include_sub && <span className={styles.muted}>（含下级）</span>}
              </span>
            ),
          },
          { title: "级别", width: 80, render: (_, a) => LEVEL_LABEL[a.permission] ?? a.permission },
          {
            title: "",
            width: 60,
            render: (_, a) =>
              canGrant ? (
                <Button
                  size="small"
                  type="link"
                  danger
                  onClick={() =>
                    revoke.mutate(a.id, {
                      onSuccess: () => message.success("已收回"),
                      onError: (e) => message.error(e.message),
                    })
                  }
                >
                  收回
                </Button>
              ) : null,
          },
        ]}
      />
      {canGrant && (
        <div style={{ display: "grid", gap: 8, marginTop: 10 }}>
          <Select
            mode="multiple"
            showSearch
            filterOption={false}
            onSearch={setQ}
            value={pickedUsers}
            onChange={setPickedUsers}
            placeholder="人员 / 数字员工（输入名字或账号搜索）"
            options={[
              { label: "人员", options: people.map((u) => ({ value: u.id, label: `${u.name}（${u.account}）` })) },
              { label: "🤖 数字员工", options: agents.map((u) => ({ value: u.id, label: u.name })) },
            ]}
          />
          <TreeSelect
            multiple
            treeData={tree}
            value={pickedDepts}
            onChange={setPickedDepts}
            placeholder="部门"
            treeDefaultExpandAll
          />
          <Space wrap>
            <Radio.Group value={level} onChange={(e) => setLevel(e.target.value)} size="small">
              <Radio.Button value="READ">使用</Radio.Button>
              <Radio.Button value="OWNER">所有者</Radio.Button>
            </Radio.Group>
            <Checkbox checked={includeSub} onChange={(e) => setIncludeSub(e.target.checked)}>
              部门含下级
            </Checkbox>
            <Button type="primary" size="small" loading={add.isPending} onClick={submit}>
              授权
            </Button>
          </Space>
          {pickedAgent && (
            <Alert type="warning" showIcon message="授给数字员工 = 授给这个 Agent 的所有使用者" description={agentHint} />
          )}
        </div>
      )}
    </>
  );
}
