"use client";

/**
 * 权限 › 授权（按对象授权）：左边选对象（组织树 / 搜人，含数字员工），右边看它的岗位、角色以及从哪来。
 * 继承来的授权写明来自哪个部门，点击跳过去；只能在来源处撤销。
 * ?subject=dept:<id> | user:<id> 直接定位（组织架构、人员详情的「授权」按钮带）。
 */
import { useQuery } from "@tanstack/react-query";
import { Alert, App, Button, Card, Empty, Modal, Segmented, Space, Switch, Table, Tree } from "antd";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";

import { buildDeptTree, PageHead, PersonAvatar, SquareIcon, StatusTag, useDirectoryDepts, UserSelect } from "@/components/common";
import { useGrantMutations } from "@/components/grantActions";
import { CheckOption, RoleChecks, usePositions } from "@/components/grantPickers";
import { Icon } from "@/components/Icon";
import { api, errorText } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { useCan } from "@/lib/session";
import type { GrantRow, User } from "@/lib/types";

type Subject = { type: "user" | "dept"; id: string };

function parseSubject(raw: string | null): Subject | null {
  if (!raw) return null;
  const [type, id] = raw.split(":");
  return (type === "user" || type === "dept") && id ? { type, id } : null;
}

export default function GrantsPage() {
  return (
    <Suspense>
      <BySubject />
    </Suspense>
  );
}

function BySubject() {
  const params = useSearchParams();
  const initial = parseSubject(params.get("subject"));
  const [mode, setMode] = useState<"dept" | "user">(initial?.type ?? "dept");
  const [subject, setSubject] = useState<Subject | null>(initial);
  const depts = useDirectoryDepts();
  const rootId = depts.data?.find((d) => !d.parent_id)?.id;
  const current = subject ?? (mode === "dept" && rootId ? { type: "dept" as const, id: rootId } : null);
  const treeData = useMemo(
    () =>
      buildDeptTree(depts.data ?? [], (d) => (
        <span className="uc-row" style={{ gap: 6 }}>
          <Icon name={d.parent_id ? "folder" : "building"} size={15} style={{ color: "var(--fg-4)" }} />
          {d.name}
        </span>
      )),
    [depts.data],
  );
  return (
    <>
      <PageHead title="授权" sub="按对象授权：选一个部门或人，看它有哪些岗位、角色，以及从哪来。" />
      <div className="uc-split">
        <div className="uc-panel">
          <div className="uc-panel-head">
            <Segmented
              block
              style={{ flex: 1 }}
              value={mode}
              onChange={(v) => (setMode(v as "dept" | "user"), setSubject(null))}
              options={[
                { value: "dept", label: "组织" },
                { value: "user", label: "人员" },
              ]}
            />
          </div>
          <div className="uc-panel-body">
            {mode === "dept" ? (
              treeData.length > 0 && (
                <Tree
                  blockNode
                  treeData={treeData}
                  defaultExpandAll
                  selectedKeys={current?.type === "dept" ? [current.id] : []}
                  onSelect={(k) => k[0] && setSubject({ type: "dept", id: String(k[0]) })}
                />
              )
            ) : (
              <div style={{ padding: 4 }}>
                <UserSelect digital value={current?.type === "user" ? current.id : undefined} onChange={(v) => v && setSubject({ type: "user", id: v as string })} />
                <div className="uc-sub" style={{ marginTop: 8 }}>
                  可以搜到数字员工（智能体的身份）。
                </div>
              </div>
            )}
          </div>
        </div>
        {current ? (
          <SubjectPanel subject={current} onJump={(s) => (setMode(s.type), setSubject(s))} />
        ) : (
          <Card>
            <Empty description="先在左边搜一个人" />
          </Card>
        )}
      </div>
    </>
  );
}

function SubjectPanel({ subject, onJump }: { subject: Subject; onJump: (s: Subject) => void }) {
  const can = useCan();
  const actions = useGrantMutations();
  const [adding, setAdding] = useState<null | "role" | "position">(null);
  const data = useQuery({
    queryKey: ["subject-grants", subject.type, subject.id],
    queryFn: () => api.get<{ own: GrantRow[]; inherited: GrantRow[] }>(`/subjects/${subject.type}/${subject.id}/grants`),
  });
  const depts = useDirectoryDepts();
  const user = useQuery({
    queryKey: ["user", subject.id],
    queryFn: () => api.get<User>(`/users/${subject.id}`),
    enabled: subject.type === "user" && can("user:view"),
  });
  const dept = depts.data?.find((d) => d.id === subject.id);
  const deptPath = (() => {
    const byId = new Map((depts.data ?? []).map((d) => [d.id, d]));
    const out: string[] = [];
    for (let cur = dept ? byId.get(dept.parent_id ?? "") : undefined; cur; cur = cur.parent_id ? byId.get(cur.parent_id) : undefined) out.unshift(cur.name);
    return out;
  })();
  const own = data.data?.own ?? [];
  const inherited = data.data?.inherited ?? [];
  const digital = user.data?.user_type === "digital";

  const table = (kind: "role" | "position") => {
    const rows = [...own, ...inherited].filter((g) => g.kind === kind);
    return (
      <Table<GrantRow>
        rowKey="id"
        pagination={false}
        loading={data.isLoading}
        dataSource={rows}
        locale={{ emptyText: kind === "role" ? "没有角色" : "没有岗位" }}
        columns={[
          {
            title: kind === "role" ? "角色" : "岗位",
            render: (_, g) => (
              <>
                <b className="uc-nw">{g.target.name}</b>
                <div className="uc-sub">{kind === "role" ? g.target.app : <span className="uc-mono">{g.target.code}</span>}</div>
              </>
            ),
          },
          {
            title: "来源",
            render: (_, g) => (
              <>
                <span className="uc-nw">
                  {own.includes(g) ? (
                    subject.type === "dept" ? (
                      `本部门 · ${g.include_sub ? "含下级部门" : "仅本部门"}`
                    ) : (
                      "直接授权"
                    )
                  ) : (
                    <>
                      继承自 <a onClick={() => onJump({ type: "dept", id: g.subject.id })}>{g.subject.name}</a>
                      {g.include_sub ? "（含下级）" : ""}
                    </>
                  )}
                </span>
                <div className="uc-sub">
                  {g.granted_by ?? "系统"} · {fmtTime(g.granted_at)}
                </div>
              </>
            ),
          },
          {
            title: "",
            align: "right",
            render: (_, g) =>
              own.includes(g) ? (
                can("grant:manage") && (
                  <span className="uc-nw">
                    {g.subject.type === "dept" && (
                      <Button type="text" size="small" onClick={() => actions.toggleSub(g)}>
                        {g.include_sub ? "改为仅本部门" : "改为含下级"}
                      </Button>
                    )}
                    <Button type="text" size="small" danger onClick={() => actions.revoke(g)}>
                      撤销
                    </Button>
                  </span>
                )
              ) : (
                <span className="uc-sub">在来源处管理</span>
              ),
          },
        ]}
      />
    );
  };

  const hint =
    subject.type === "dept"
      ? "授予本部门的岗位和角色，部门成员自动获得；选「含下级」时下级部门的成员也获得。"
      : digital
        ? "数字员工只认直接授给它的角色和岗位，授给部门的对它不生效。"
        : "有效授权 = 直接授给本人的 + 所在部门的 + 上级部门「含下级」的。";

  return (
    <Space direction="vertical" size={14} style={{ width: "100%", minWidth: 0 }}>
      <Card>
        <div className="uc-row" style={{ gap: 12 }}>
          {subject.type === "dept" ? (
            <>
              <SquareIcon icon="building" size={40} />
              <div className="uc-grow">
                <div className="uc-sub">{deptPath.join(" / ") || "根部门"}</div>
                <h2 style={{ margin: 0, fontSize: 18 }}>{dept?.name}</h2>
              </div>
            </>
          ) : (
            <>
              {digital ? <SquareIcon icon="bot" size={40} color="#0ea5e9" /> : <PersonAvatar name={user.data?.name ?? "?"} size={40} />}
              <div className="uc-grow">
                <div className="uc-row">
                  <h2 style={{ margin: 0, fontSize: 18 }}>{user.data?.name ?? "用户"}</h2>
                  {user.data && <StatusTag status={user.data.status} />}
                </div>
                <div className="uc-sub">
                  {user.data?.account} · {digital ? `负责人 ${user.data?.owner?.name ?? "—"}` : user.data?.dept_path?.join(" / ")}
                </div>
              </div>
            </>
          )}
          {can("grant:manage") && (
            <Space>
              <Button icon={<Icon name="briefcase" size={14} />} onClick={() => setAdding("position")}>
                授予岗位
              </Button>
              <Button type="primary" icon={<Icon name="plus" size={14} />} onClick={() => setAdding("role")}>
                授予角色
              </Button>
            </Space>
          )}
        </div>
        <div className="uc-sub" style={{ marginTop: 10 }}>
          {hint}
        </div>
      </Card>
      <Card size="small" title="岗位" extra={<span className="uc-sub">岗位的默认角色随岗位一起生效</span>} styles={{ body: { padding: 0 } }}>
        {table("position")}
      </Card>
      <Card size="small" title="角色" styles={{ body: { padding: 0 } }}>
        {table("role")}
      </Card>
      {subject.type === "user" && can("user:view") && (
        <div style={{ textAlign: "right" }}>
          <Link href={`/${digital ? "digital" : "users"}/${subject.id}`}>查看在各应用的有效角色与来源 →</Link>
        </div>
      )}
      {adding && (
        <GrantItemsModal
          kind={adding}
          subject={subject}
          subjectName={subject.type === "dept" ? (dept?.name ?? "") : (user.data?.name ?? "")}
          granted={own.filter((g) => g.kind === adding).map((g) => g.target.id)}
          onClose={() => setAdding(null)}
          onDone={actions.refresh}
        />
      )}
    </Space>
  );
}

function GrantItemsModal({
  kind,
  subject,
  subjectName,
  granted,
  onClose,
  onDone,
}: {
  kind: "role" | "position";
  subject: Subject;
  subjectName: string;
  granted: string[];
  onClose: () => void;
  onDone: () => void;
}) {
  const { message } = App.useApp();
  const [ids, setIds] = useState<string[]>([]);
  const [includeSub, setIncludeSub] = useState(true);
  const positions = usePositions(kind === "position");
  async function ok() {
    if (!ids.length) return message.warning(`请选择要授予的${kind === "role" ? "角色" : "岗位"}`);
    try {
      const r = await api.post<{ added: number; updated: number }>("/grants", { kind, target_ids: ids, subjects: [subject], include_sub: includeSub });
      onDone();
      message.success(`已授予 ${r.added} 项；应用刷新授权后生效`);
      onClose();
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <Modal open title={`${kind === "role" ? "授予角色" : "授予岗位"} · ${subjectName}`} onCancel={onClose} onOk={ok} okText="授权" width={600}>
      {subject.type === "dept" && (
        <Alert
          style={{ marginBottom: 12 }}
          type="info"
          message={
            <span className="uc-row">
              <span className="uc-grow">
                <b>含下级部门</b>
                <span className="uc-sub" style={{ marginLeft: 8 }}>
                  下级部门的成员也获得
                </span>
              </span>
              <Switch size="small" checked={includeSub} onChange={setIncludeSub} />
            </span>
          }
        />
      )}
      {kind === "role" ? (
        <RoleChecks value={ids} onChange={setIds} disabled={granted} />
      ) : (
        <div style={{ maxHeight: 360, overflow: "auto" }}>
          {(positions.data ?? []).map((p) => (
            <CheckOption
              key={p.id}
              checked={ids.includes(p.id)}
              granted={granted.includes(p.id)}
              onChange={(on) => setIds(on ? [...ids, p.id] : ids.filter((x) => x !== p.id))}
              name={p.name}
              desc={`默认角色：${p.roles.map((r) => `${r.app} · ${r.name}`).join("、") || "无"}`}
            />
          ))}
          {!positions.data?.length && <Empty description="还没有岗位" />}
        </div>
      )}
    </Modal>
  );
}
