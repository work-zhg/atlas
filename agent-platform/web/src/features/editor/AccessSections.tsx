"use client";

/**
 * 编辑器里与权限相关的三节（doc/digital-employee-design.html）：
 *
 *   权限类型  创建时选；之后只有所有者能切换（§7.1）
 *   身份      数字员工、所配知识库它能不能读（§6.1）—— 能用这个智能体的人都能看
 *   共享      谁能用 / 改它：用户中心数据权限 AtlasAgent（§8.1），所有者维护
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Card, Checkbox, Descriptions, Radio, Select, Space, Table, Tag, TreeSelect, Typography } from "antd";
import { useMemo, useState } from "react";

import {
  type AgentGrant,
  type AgentLevel,
  agentGrantsApi,
  useAgentGrants,
  useAgentIdentity,
  useSetAuthSubject,
} from "@/api/agents";
import type { AgentDetail, AuthSubject } from "@/api/types";
import { type DirDept, directory } from "@/features/knowledge/api";

export const AUTH_SUBJECT_META: Record<AuthSubject, { label: string; desc: string }> = {
  user: {
    label: "以人为权限",
    desc: "按使用者本人的权限访问知识库、MCP 等资源：每个人只能经它拿到自己本来就能拿到的，结果因人而异。适合个人助手。",
  },
  agent: {
    label: "以 Agent 为权限",
    desc: "按这个智能体的数字员工在用户中心的授权干活，不看使用者本人的权限：谁来用结果都一样。适合岗位型智能体、流程节点。能用它的人都能间接用到这些权限，编辑只限所有者。",
  },
};

const LEVEL_LABEL: Record<AgentLevel, string> = { READ: "使用", WRITE: "编辑", OWNER: "所有者" };
const LEVEL_OPTS = (["READ", "WRITE", "OWNER"] as const).map((l) => ({ value: l, label: LEVEL_LABEL[l] }));

/** 新建时选权限类型（必选，默认以人为权限）。 */
export function AuthSubjectPicker({
  value,
  onChange,
  disabled,
}: {
  value: AuthSubject;
  onChange: (v: AuthSubject) => void;
  disabled?: boolean;
}) {
  return (
    <Radio.Group
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value as AuthSubject)}
      style={{ display: "flex", flexDirection: "column", gap: 8 }}
    >
      {(Object.keys(AUTH_SUBJECT_META) as AuthSubject[]).map((k) => (
        <Radio key={k} value={k}>
          <b>{AUTH_SUBJECT_META[k].label}</b>
          <div style={{ fontSize: 12, color: "var(--fg-3)", marginTop: 2 }}>{AUTH_SUBJECT_META[k].desc}</div>
        </Radio>
      ))}
    </Radio.Group>
  );
}

/** 已有智能体：当前权限类型；所有者可切换（只影响之后新建的会话轮次）。 */
export function AuthSubjectSwitch({ detail }: { detail: AgentDetail }) {
  const { modal, message } = App.useApp();
  const setSubject = useSetAuthSubject(detail.id);
  const current = detail.auth_subject;
  const isOwner = detail.my_level === "OWNER";
  const next: AuthSubject = current === "agent" ? "user" : "agent";

  const confirm = () =>
    modal.confirm({
      title: `改为「${AUTH_SUBJECT_META[next].label}」？`,
      content:
        next === "agent"
          ? "之后所有使用者都按数字员工的授权干活，与本人权限无关；数字员工没有被授权的资源（知识库、MCP）将不可用。进行中的会话轮次不受影响。"
          : "之后按每个使用者本人的权限干活，结果因人而异。进行中的会话轮次不受影响。",
      onOk: async () => {
        try {
          await setSubject.mutateAsync(next);
          message.success("已切换权限类型");
        } catch (e) {
          message.error((e as Error).message);
        }
      },
    });

  return (
    <Space direction="vertical" size={6}>
      <Space>
        <Tag color={current === "agent" ? "purple" : "blue"}>{AUTH_SUBJECT_META[current].label}</Tag>
        {isOwner && (
          <Button size="small" onClick={confirm} loading={setSubject.isPending}>
            改为{AUTH_SUBJECT_META[next].label}
          </Button>
        )}
      </Space>
      <span style={{ fontSize: 12, color: "var(--fg-3)" }}>{AUTH_SUBJECT_META[current].desc}</span>
    </Space>
  );
}

/** 身份：数字员工与它对所配知识库的权限摘要。 */
export function IdentityPanel({ agentId }: { agentId: string }) {
  const q = useAgentIdentity(agentId);
  const ident = q.data;
  if (!ident) return null;
  const de = ident.digital_employee;
  return (
    <Space direction="vertical" size={10} style={{ width: "100%" }}>
      {ident.notices.map((n) => (
        <Alert key={n} type="warning" showIcon message={n} />
      ))}
      <Descriptions size="small" column={1} bordered>
        <Descriptions.Item label="数字员工">
          {de ? (
            <Space>
              <span>🤖 {de.name ?? "—"}</span>
              <Typography.Text type="secondary" copyable={{ text: de.id }}>
                {de.account ?? de.id}
              </Typography.Text>
              {de.status === "disabled" && <Tag>已停用</Tag>}
            </Space>
          ) : (
            <Typography.Text type="secondary">尚未建立（未接入用户中心或待回填）</Typography.Text>
          )}
        </Descriptions.Item>
        <Descriptions.Item label="所配知识库">
          {ident.knowledge.length === 0 ? (
            <Typography.Text type="secondary">没有</Typography.Text>
          ) : (
            <Space wrap>
              {ident.knowledge.map((k) => (
                <Tag key={k.id} color={k.readable === false ? "error" : k.readable ? "success" : undefined}>
                  {k.name ?? k.id}
                  {k.readable === false && " · 数字员工读不到"}
                </Tag>
              ))}
            </Space>
          )}
        </Descriptions.Item>
      </Descriptions>
      {ident.auth_subject === "user" && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          以人为权限：运行时按每个使用者本人的权限判断，使用者读不到的库会被跳过并提示。
        </Typography.Text>
      )}
    </Space>
  );
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

/** 共享：谁能用 / 改这个智能体。使用 = 能对话；编辑 = 改以人为权限的配置；所有者 = 一切含共享。 */
export function SharingPanel({ detail }: { detail: AgentDetail }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const grants = useAgentGrants(detail.id);
  const manage = grants.data?.my_level === "OWNER";
  const depts = useQuery({ queryKey: ["directory", "depts"], queryFn: directory.depts, staleTime: 60_000, enabled: manage });
  const [q, setQ] = useState("");
  const users = useQuery({
    queryKey: ["directory", "users", q],
    queryFn: () => directory.users(q),
    staleTime: 30_000,
    enabled: manage,
  });
  const [level, setLevel] = useState<AgentLevel>("READ");
  const [pickedUsers, setPickedUsers] = useState<string[]>([]);
  const [pickedDepts, setPickedDepts] = useState<string[]>([]);
  const [includeSub, setIncludeSub] = useState(true);
  const tree = useMemo(() => deptTree(depts.data ?? []), [depts.data]);

  const act = async (fn: () => Promise<unknown>, ok: string) => {
    try {
      await fn();
      await qc.invalidateQueries({ queryKey: ["agents", "grants", detail.id] });
      message.success(ok);
    } catch (e) {
      message.error((e as Error).message);
    }
  };

  async function add() {
    const subjects = [
      ...pickedDepts.map((id) => ({ type: "dept" as const, id })),
      ...pickedUsers.map((id) => ({ type: "user" as const, id })),
    ];
    if (!subjects.length) return message.warning("请选择人员或部门");
    await act(() => agentGrantsApi.add(detail.id, { level, subjects, include_sub: includeSub }), "已共享");
    setPickedUsers([]);
    setPickedDepts([]);
  }

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {detail.auth_subject === "agent" && (
        <Alert
          type="warning"
          showIcon
          message="以 Agent 为权限：共享给谁，谁就能经它用到数字员工的全部权限（知识库、MCP 等），即使本人没有。"
        />
      )}
      <Table<AgentGrant>
        size="small"
        rowKey="id"
        loading={grants.isLoading}
        pagination={false}
        dataSource={grants.data?.data ?? []}
        columns={[
          {
            title: "对象",
            render: (_, r) =>
              r.subject.type === "USER" ? (
                <Space>
                  <b>
                    {r.subject.user_type === "digital" && "🤖 "}
                    {r.subject.name ?? r.subject.id}
                  </b>
                  {r.subject.account && <span style={{ color: "var(--fg-3)" }}>{r.subject.account}</span>}
                  {r.subject.status === "disabled" && <Tag>已停用</Tag>}
                </Space>
              ) : (
                <b>🏢 {r.subject.name ?? r.subject.id}</b>
              ),
          },
          {
            title: "权限",
            render: (_, r) =>
              manage ? (
                <Select
                  size="small"
                  value={r.permission}
                  style={{ width: 100 }}
                  options={LEVEL_OPTS}
                  onChange={(v) => act(() => agentGrantsApi.update(detail.id, r.id, { level: v }), "已调整")}
                />
              ) : (
                <Tag>{LEVEL_LABEL[r.permission]}</Tag>
              ),
          },
          { title: "范围", render: (_, r) => (r.subject.type === "USER" ? "本人" : r.include_sub ? "本部门及下级部门" : "仅本部门") },
          {
            title: "",
            align: "right",
            render: (_, r) =>
              manage && (
                <Button type="link" size="small" danger onClick={() => act(() => agentGrantsApi.revoke(detail.id, r.id), "已移除")}>
                  移除
                </Button>
              ),
          },
        ]}
      />
      {manage && (
        <Card size="small" title="添加共享">
          <Space direction="vertical" style={{ width: "100%" }}>
            <Radio.Group value={level} onChange={(e) => setLevel(e.target.value)} options={LEVEL_OPTS} />
            <TreeSelect
              multiple
              allowClear
              treeDefaultExpandAll
              placeholder="选择部门"
              style={{ width: "100%" }}
              value={pickedDepts}
              onChange={setPickedDepts}
              treeData={tree}
            />
            <Checkbox checked={includeSub} onChange={(e) => setIncludeSub(e.target.checked)}>
              包含下级部门
            </Checkbox>
            <Select
              mode="multiple"
              showSearch
              filterOption={false}
              onSearch={setQ}
              placeholder="搜索人员（姓名 / 账号）"
              style={{ width: "100%" }}
              value={pickedUsers}
              onChange={setPickedUsers}
              options={(users.data ?? []).map((u) => ({ value: u.id, label: `${u.name}（${u.account}）` }))}
            />
            <Button type="primary" onClick={add}>
              共享
            </Button>
          </Space>
        </Card>
      )}
    </Space>
  );
}
