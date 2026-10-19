"use client";

/**
 * 知识库共享 = 用户中心数据权限（数据编码 AtlasKnowledgeBase）。
 * 只读：检索、召回测试；可编辑：上传、编辑分段、改检索设置；所有者：删除、调整共享。
 * 授给部门时可含下级；「全部知识库」的授权在用户中心维护，不在这里。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Card, Checkbox, Radio, Select, Space, Table, Tag, TreeSelect, Typography } from "antd";
import { useMemo, useState } from "react";

import { type Dataset, type DirDept, type KbGrant, type KbLevel, LEVEL_LABEL, directory, kbApi, useGrants } from "./api";

type Level = Exclude<KbLevel, "NONE">;
const LEVEL_OPTS = (["READ", "WRITE", "OWNER"] as const).map((l) => ({ value: l, label: LEVEL_LABEL[l] }));

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

export function SharingTab({ ds }: { ds: Dataset }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const grants = useGrants(ds.id);
  const depts = useQuery({ queryKey: ["directory", "depts"], queryFn: directory.depts, staleTime: 60_000 });
  const [q, setQ] = useState("");
  const users = useQuery({ queryKey: ["directory", "users", q], queryFn: () => directory.users(q), staleTime: 30_000 });
  const [level, setLevel] = useState<Level>("READ");
  const [pickedUsers, setPickedUsers] = useState<string[]>([]);
  const [pickedDepts, setPickedDepts] = useState<string[]>([]);
  const [includeSub, setIncludeSub] = useState(true);
  const tree = useMemo(() => deptTree(depts.data ?? []), [depts.data]);
  const g = grants.data;
  const manage = !!g?.can_manage;

  const act = async (fn: () => Promise<unknown>, ok: string) => {
    try {
      await fn();
      await qc.invalidateQueries({ queryKey: ["kb", "grants", ds.id] });
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
    await act(() => kbApi.addGrants(ds.id, { level, subjects, include_sub: includeSub }), "已共享");
    setPickedUsers([]);
    setPickedDepts([]);
  }

  if (g && !g.shareable) {
    return <Alert type="info" showIcon message="这个库由服务账号创建，只有该服务账号（及拥有「全部知识库」权限的人）能用，不能共享。" />;
  }

  return (
    <Space direction="vertical" size={16} style={{ width: "100%", maxWidth: 820 }}>
      <Typography.Text type="secondary">
        你的权限：<Tag>{LEVEL_LABEL[g?.my_level ?? "NONE"]}</Tag>
        只读可检索与召回测试；可编辑还能上传文档、编辑分段、改检索设置；所有者还能删除库和调整共享。
      </Typography.Text>
      <Table<KbGrant>
        size="small"
        rowKey="id"
        loading={grants.isLoading}
        pagination={false}
        dataSource={g?.data ?? []}
        columns={[
          {
            title: "对象",
            render: (_, r) =>
              r.subject.type === "USER" ? (
                <Space>
                  <b>{r.subject.name ?? r.subject.id}</b>
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
                  onChange={(v) => act(() => kbApi.updateGrant(ds.id, r.id, { level: v }), "已调整")}
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
                <Button type="link" size="small" danger onClick={() => act(() => kbApi.revokeGrant(ds.id, r.id), "已移除")}>
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
