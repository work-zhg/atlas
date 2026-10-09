"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Card, Checkbox, Descriptions, Empty, Form, Input, Modal, Select, Space, Table, Tag, Tree, TreeSelect } from "antd";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { buildDeptTree, PageHead, StatusTag, UserSelect } from "@/components/common";
import { UserForm } from "@/components/UserForm";
import { api, errorText } from "@/lib/api";
import { useCan } from "@/lib/session";
import type { DeptDetail, DeptNode, Page, User } from "@/lib/types";

export default function OrgPage() {
  const can = useCan();
  const router = useRouter();
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const tree = useQuery({ queryKey: ["org", "tree"], queryFn: () => api.get<DeptNode[]>("/depts/tree") });
  const [sel, setSel] = useState<string>();
  const [includeSub, setIncludeSub] = useState(true);
  const rootId = tree.data?.find((d) => !d.parent_id)?.id;
  const current = sel ?? rootId;
  useEffect(() => {
    if (sel && tree.data && !tree.data.some((d) => d.id === sel)) setSel(undefined);
  }, [tree.data, sel]);

  const detail = useQuery({
    queryKey: ["org", "dept", current],
    queryFn: () => api.get<DeptDetail>(`/depts/${current}`),
    enabled: !!current,
  });
  const members = useQuery({
    queryKey: ["org", "members", current, includeSub],
    queryFn: () => api.get<Page<User>>(`/depts/${current}/members`, { include_sub: includeSub, size: 200 }),
    enabled: !!current,
  });

  const [form, setForm] = useState<null | "new" | "edit">(null);
  const [userForm, setUserForm] = useState(false);
  const [moveIn, setMoveIn] = useState(false);
  const refresh = () => qc.invalidateQueries({ queryKey: ["org"] });

  const treeData = useMemo(
    () =>
      buildDeptTree(tree.data ?? [], (d: DeptNode) => (
        <span>
          {d.parent_id ? "📁" : "🏢"} {d.name} <span className="uc-muted">{d.total_count}</span>
        </span>
      )),
    [tree.data],
  );

  const setLeader = useMutation({
    mutationFn: (uid: string | null) => api.patch(`/depts/${current}`, { leader_id: uid }),
    onSuccess: () => {
      refresh();
      message.success("负责人已更新");
    },
    onError: (e) => message.error(errorText(e)),
  });

  async function remove() {
    if (!detail.data) return;
    try {
      const impact = await api.del<{ app_scopes: string[]; grants: number; data_acls: number }>(`/depts/${detail.data.id}`, { dry_run: true });
      modal.confirm({
        title: `删除部门 · ${detail.data.name}`,
        content: (
          <div>
            确定删除「{detail.data.path.join(" / ")}」？
            {impact.app_scopes.length > 0 && <Alert style={{ marginTop: 8 }} type="warning" message={`将从 ${impact.app_scopes.join("、")} 的可访问范围中移除`} />}
            {(impact.grants > 0 || impact.data_acls > 0) && (
              <Alert style={{ marginTop: 8 }} type="warning" message={`将撤销授予该部门的 ${impact.grants} 条角色授权、${impact.data_acls} 条数据授权`} />
            )}
          </div>
        ),
        okText: "删除",
        okButtonProps: { danger: true },
        onOk: async () => {
          await api.del(`/depts/${detail.data!.id}`);
          setSel(detail.data!.parent_id ?? undefined);
          refresh();
          message.success("已删除");
        },
      });
    } catch (e) {
      message.error(errorText(e));
    }
  }

  const d = detail.data;
  return (
    <>
      <PageHead title="组织架构" sub="维护部门树、负责人与部门成员；直属上级按负责人自动计算。" />
      <div style={{ display: "grid", gridTemplateColumns: "300px minmax(0,1fr)", gap: 16, alignItems: "start" }}>
        <Card title="部门" size="small" extra={<span className="uc-muted">{tree.data?.length ?? 0} 个</span>}>
          {treeData.length > 0 && (
            <Tree
              treeData={treeData}
              defaultExpandAll
              selectedKeys={current ? [current] : []}
              onSelect={(keys) => keys[0] && setSel(String(keys[0]))}
            />
          )}
        </Card>
        <Space direction="vertical" size={16} style={{ width: "100%" }}>
          <Card loading={!d}>
            {d && (
              <>
                <div style={{ display: "flex", alignItems: "flex-start", gap: 8, flexWrap: "wrap" }}>
                  <div>
                    <div className="uc-muted">{d.path.join(" / ")}</div>
                    <h2 style={{ margin: "2px 0 0", fontSize: 19 }}>{d.name}</h2>
                  </div>
                  <div className="uc-grow" />
                  {can("grant:view") && <Button onClick={() => router.push(`/grants?subject=dept:${d.id}`)}>授权</Button>}
                  {can("org:manage") && <Button onClick={() => setForm("new")}>＋ 新建子部门</Button>}
                  {can("org:manage") && <Button onClick={() => setForm("edit")}>编辑</Button>}
                  {can("org:manage") && d.parent_id && (
                    <Button danger onClick={remove}>
                      删除
                    </Button>
                  )}
                </div>
                <Descriptions column={1} size="small" style={{ marginTop: 16 }}>
                  <Descriptions.Item label="负责人">
                    {d.leader ? (
                      <Space>
                        {d.leader.name}
                        {d.leader.status === "disabled" && <Tag color="orange">负责人已停用，请更换</Tag>}
                      </Space>
                    ) : (
                      <span className="uc-muted">未设置{d.parent_id ? "（成员的直属上级取上级部门负责人）" : ""}</span>
                    )}
                  </Descriptions.Item>
                  <Descriptions.Item label="直属成员 / 含子部门">
                    {d.direct_count} 人 / {d.total_count} 人
                  </Descriptions.Item>
                  <Descriptions.Item label="子部门">
                    {d.children.length ? (
                      d.children.map((c) => (
                        <Tag key={c.id} style={{ cursor: "pointer" }} onClick={() => setSel(c.id)}>
                          {c.name}
                        </Tag>
                      ))
                    ) : (
                      <span className="uc-muted">无</span>
                    )}
                  </Descriptions.Item>
                </Descriptions>
              </>
            )}
          </Card>
          <Card
            title={
              <Space>
                成员 <span className="uc-muted">{members.data?.total ?? 0} 人</span>
                <Checkbox checked={includeSub} onChange={(e) => setIncludeSub(e.target.checked)}>
                  包含子部门
                </Checkbox>
              </Space>
            }
            extra={
              <Space>
                {can("org:manage") && <Button onClick={() => setMoveIn(true)}>调入成员</Button>}
                {can("user:create") && (
                  <Button type="primary" onClick={() => setUserForm(true)}>
                    ＋ 新建用户
                  </Button>
                )}
              </Space>
            }
          >
            <Table<User>
              rowKey="id"
              size="small"
              loading={members.isLoading}
              dataSource={members.data?.items ?? []}
              pagination={false}
              locale={{ emptyText: <Empty description="该部门还没有成员" /> }}
              columns={[
                {
                  title: "成员",
                  render: (_, u) => (
                    <Space>
                      <Link href={`/users/${u.id}`}>{u.name}</Link>
                      <span className="uc-muted">{u.account}</span>
                      {u.is_leader && <Tag color="purple">负责人</Tag>}
                    </Space>
                  ),
                },
                { title: "岗位", render: (_, u) => u.positions?.join("、") || <span className="uc-muted">—</span> },
                { title: "状态", render: (_, u) => <StatusTag status={u.status} /> },
                {
                  title: "",
                  align: "right",
                  render: (_, u) =>
                    can("org:manage") && !u.is_leader && u.status !== "disabled" ? (
                      <Button type="link" size="small" onClick={() => setLeader.mutate(u.id)}>
                        设为负责人
                      </Button>
                    ) : null,
                },
              ]}
            />
          </Card>
        </Space>
      </div>
      {d && form && <DeptForm mode={form} dept={d} tree={tree.data ?? []} onClose={() => setForm(null)} onCreated={setSel} />}
      <UserForm open={userForm} presetDept={current} onClose={() => setUserForm(false)} />
      {d && moveIn && <MoveInModal dept={d} onClose={() => setMoveIn(false)} />}
    </>
  );
}

function DeptForm({
  mode,
  dept,
  tree,
  onClose,
  onCreated,
}: {
  mode: "new" | "edit";
  dept: DeptDetail;
  tree: DeptNode[];
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const [form] = Form.useForm();
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const editing = mode === "edit";
  const candidates = useQuery({
    queryKey: ["org", "members", dept.id, true, "leader"],
    queryFn: () => api.get<Page<User>>(`/depts/${dept.id}/members`, { include_sub: true, size: 200 }),
    enabled: editing,
  });
  // 上级下拉排除自己及全部下级
  const exclude = useMemo(() => {
    const out = new Set<string>([dept.id]);
    let grew = true;
    while (grew) {
      grew = false;
      for (const d of tree) if (d.parent_id && out.has(d.parent_id) && !out.has(d.id)) out.add(d.id), (grew = true);
    }
    return out;
  }, [tree, dept.id]);

  async function submit(v: { name: string; parent_id?: string; leader_id?: string | null }) {
    try {
      if (!editing) {
        const r = await api.post<DeptDetail>("/depts", { parent_id: dept.id, name: v.name });
        qc.invalidateQueries({ queryKey: ["org"] });
        message.success(`已新建部门「${v.name}」`);
        onCreated(r.id);
        onClose();
        return;
      }
      await api.patch(`/depts/${dept.id}`, { name: v.name, leader_id: v.leader_id ?? null, version: dept.version });
      if (dept.parent_id && v.parent_id && v.parent_id !== dept.parent_id) {
        const pre = await api.post<{ dept_count: number; member_count: number; leaders_to_clear: string[]; path_after: string }>(
          `/depts/${dept.id}/move`,
          { parent_id: v.parent_id },
          { dry_run: true },
        );
        await new Promise<void>((resolve, reject) =>
          modal.confirm({
            title: "移动部门",
            content: (
              <div>
                将连同 {pre.dept_count} 个部门、{pre.member_count} 名成员移动到「{pre.path_after}」。
                {pre.leaders_to_clear.length > 0 && (
                  <Alert style={{ marginTop: 8 }} type="warning" message={`将自动清空「${pre.leaders_to_clear.join("」「")}」的负责人`} />
                )}
                <div className="uc-muted" style={{ marginTop: 8 }}>
                  授权与应用访问将按新位置重新计算；接入应用在刷新授权快照后生效。
                </div>
              </div>
            ),
            onOk: async () => {
              await api.post(`/depts/${dept.id}/move`, { parent_id: v.parent_id });
              resolve();
            },
            onCancel: () => reject(new Error("已取消移动")),
          }),
        );
      }
      qc.invalidateQueries({ queryKey: ["org"] });
      message.success("部门已保存");
      onClose();
    } catch (e) {
      if (!(e instanceof Error && e.message === "已取消移动")) message.error(errorText(e));
      else qc.invalidateQueries({ queryKey: ["org"] });
    }
  }

  const parentTree = buildDeptTree(tree.filter((d) => !exclude.has(d.id)));
  return (
    <Modal open title={editing ? `编辑部门 · ${dept.name}` : `新建子部门 · ${dept.name}`} onCancel={onClose} onOk={() => form.submit()} okText="保存" destroyOnHidden>
      <Form
        form={form}
        layout="vertical"
        onFinish={submit}
        initialValues={editing ? { name: dept.name, parent_id: dept.parent_id, leader_id: dept.leader?.id ?? null } : {}}
      >
        <Form.Item name="name" label="部门名称" rules={[{ required: true, message: "请填写部门名称" }, { pattern: /^[^/]+$/, message: "不能包含「/」" }]}>
          <Input maxLength={50} />
        </Form.Item>
        {editing && dept.parent_id && (
          <Form.Item name="parent_id" label="上级部门" extra="移动部门会连同其子部门与成员一起移动。">
            <TreeSelect treeData={parentTree} treeDefaultExpandAll />
          </Form.Item>
        )}
        {editing ? (
          <Form.Item name="leader_id" label="负责人" extra="只能从本部门及下级部门的未停用成员中选择；直属上级按负责人计算">
            <Select
              allowClear
              placeholder="不设置"
              options={(candidates.data?.items ?? [])
                .filter((u) => u.status !== "disabled")
                .map((u) => ({ value: u.id, label: `${u.name}（${u.account}）` }))}
            />
          </Form.Item>
        ) : (
          <div className="uc-muted">新部门还没有成员，调入成员后再设置负责人。</div>
        )}
      </Form>
    </Modal>
  );
}

function MoveInModal({ dept, onClose }: { dept: DeptDetail; onClose: () => void }) {
  const [ids, setIds] = useState<string[]>([]);
  const qc = useQueryClient();
  const { message } = App.useApp();
  async function ok() {
    if (!ids.length) return message.warning("请选择成员");
    try {
      const r = await api.post<{ moved: number; leaders_to_clear: string[] }>(`/depts/${dept.id}/members/move-in`, { user_ids: ids });
      qc.invalidateQueries({ queryKey: ["org"] });
      qc.invalidateQueries({ queryKey: ["users"] });
      if (r.leaders_to_clear.length) message.warning(`已调入 ${r.moved} 人；已清空「${r.leaders_to_clear.join("」「")}」的负责人`);
      else message.success(`已调入 ${r.moved} 人`);
      onClose();
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <Modal open title={`调入成员 · ${dept.name}`} onCancel={onClose} onOk={ok} okText="调入">
      <UserSelect multiple value={ids} onChange={(v) => setIds(v as string[])} />
      <div className="uc-muted" style={{ marginTop: 8 }}>
        调入后，成员的部门变更为「{dept.name}」。
      </div>
    </Modal>
  );
}
