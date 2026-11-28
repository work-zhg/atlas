"use client";

/**
 * 项目 · 配置：绑定流程模板，给模板里的每个角色分配成员和数字员工。只有团队管理员能改。
 *
 * ★ 角色按模板分开配（项目 × 模板 × 角色）；成员只能选本团队读写以上的人，数字员工只能选本团队接入的。
 * ★ 分配后后端以接入人的身份给这些人授数字员工的使用权（Agent 平台只认数据权限），失败只给警告。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Input, Modal, Popconfirm, Select } from "antd";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { Crumb, Empty, Icon, LoadError, Loading, Page, PageHead } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { atLeast, LEVEL } from "@/lib/format";
import { useProject, useTeamAgents, useTeamMembers, useTemplates } from "@/lib/queries";
import { roleDuties } from "@/lib/structure";
import { useCan, useFollowTeam } from "@/lib/team";
import type { Binding, ProjectDetail, Structure, TeamAgent, TeamMember } from "@/lib/types";

export default function ProjectConfig() {
  const { id } = useParams<{ id: string }>();
  const project = useProject(id);
  useFollowTeam(project.data?.team_id);
  const can = useCan();
  const members = useTeamMembers(project.data?.team_id);
  const agents = useTeamAgents(project.data?.team_id);
  const [binding, setBinding] = useState(false);

  if (project.error) return <LoadError error={project.error} onRetry={() => void project.refetch()} />;
  if (!project.data) return <Loading />;
  const p = project.data;
  const adm = can.configFlow;

  return (
    <Page>
      <Crumb items={[[p.name, `/projects/${p.id}`], ["配置", undefined]]} />
      <PageHead title={`${p.name} · 配置`} />
      {!adm && (
        <div className="banner info" style={{ marginBottom: 12 }}>
          <Icon name="lock" size={15} />
          <div>项目配置只有团队管理员能改</div>
        </div>
      )}
      <BasicInfo project={p} editable={adm} />
      <div className="row" style={{ margin: "18px 0 10px" }}>
        <h2>流程模板</h2>
        <span className="sm muted">给每个角色配好人和数字员工，就能在这个项目里发起</span>
        <span className="sp" />
        {adm && (
          <Button icon={<Icon name="plus" size={14} />} onClick={() => setBinding(true)}>
            绑定流程模板
          </Button>
        )}
      </div>
      {p.bindings.length ? (
        p.bindings.map((b) => (
          <BindCard key={b.template.id} project={p} binding={b} editable={adm} members={members.data?.members ?? []} agents={agents.data ?? []} />
        ))
      ) : (
        <div className="card">
          <Empty icon="share" title="还没绑定流程模板">
            绑定后给角色分配成员和数字员工，就能在这个项目里发起
          </Empty>
        </div>
      )}
      {binding && <BindModal project={p} onClose={() => setBinding(false)} />}
    </Page>
  );
}

function BasicInfo({ project, editable }: { project: ProjectDetail; editable: boolean }) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const [edit, setEdit] = useState(false);
  const [name, setName] = useState(project.name);
  const [desc, setDesc] = useState(project.description ?? "");
  const save = async () => {
    try {
      await api.patch(`/projects/${project.id}`, { name: name.trim(), description: desc.trim() });
      await qc.invalidateQueries({ queryKey: ["project", project.id] });
      await qc.invalidateQueries({ queryKey: ["team", project.team_id, "projects"] });
      setEdit(false);
    } catch (e) {
      message.error(errorText(e));
    }
  };
  return (
    <div className="card pad">
      <div className="row" style={{ marginBottom: 10 }}>
        <h2>基本信息</h2>
        <span className="sp" />
        {editable && !edit && (
          <Button size="small" onClick={() => setEdit(true)}>
            编辑
          </Button>
        )}
      </div>
      {edit ? (
        <div className="col" style={{ maxWidth: 520, gap: 10 }}>
          <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={64} />
          <Input.TextArea value={desc} onChange={(e) => setDesc(e.target.value)} maxLength={200} autoSize={{ minRows: 2 }} placeholder="说明" />
          <div className="row">
            <span className="sp" />
            <Button onClick={() => setEdit(false)}>取消</Button>
            <Button type="primary" onClick={() => void save()} disabled={!name.trim()}>
              保存
            </Button>
          </div>
        </div>
      ) : (
        <dl className="kv">
          <dt>名称</dt>
          <dd>{project.name}</dd>
          <dt>说明</dt>
          <dd>{project.description || "—"}</dd>
        </dl>
      )}
    </div>
  );
}

function BindCard({
  project,
  binding: b,
  editable,
  members,
  agents,
}: {
  project: ProjectDetail;
  binding: Binding;
  editable: boolean;
  members: TeamMember[];
  agents: TeamAgent[];
}) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const t = b.template;
  const version = useQuery({
    queryKey: ["template", t.id, "version", b.version_no],
    queryFn: () => api.get<{ structure: Structure }>(`/templates/${t.id}/versions/${b.version_no}`),
    enabled: b.version_no > 0,
    staleTime: Infinity,
  });
  const writers = members.filter((m) => atLeast(m.level, "WRITE"));
  const nameOf = (uid: string) => members.find((m) => m.id === uid)?.name ?? `${uid.slice(0, 6)}…`;

  const assign = async (role: string, next: { members: string[]; agent: string | null }) => {
    try {
      const r = await api.put<{ ok: boolean; warnings: string[] }>(`/projects/${project.id}/templates/${t.id}/roles/${role}`, next);
      for (const w of r.warnings ?? []) message.warning(w);
      await qc.invalidateQueries({ queryKey: ["project", project.id] });
    } catch (e) {
      message.error(errorText(e));
    }
  };
  const unbind = async () => {
    try {
      await api.del(`/projects/${project.id}/templates/${t.id}`);
      await qc.invalidateQueries({ queryKey: ["project", project.id] });
    } catch (e) {
      message.error(errorText(e));
    }
  };

  const missingOf = (role: string) => b.missing.filter((m) => m.role === role).map((m) => m.what);
  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <div className="row" style={{ padding: "12px 16px", borderBottom: "1px solid var(--tf-line-soft)" }}>
        <span className="ic2" style={{ background: "var(--primary)", width: 30, height: 30, borderRadius: 8 }}>
          <Icon name="share" size={15} />
        </span>
        <div className="grow">
          <b>{t.name}</b>
          <div className="xs faint">{b.version_no ? `用最新发布的 v${b.version_no}；已发起的流程不受之后的修改影响` : "还没发布"}</div>
        </div>
        {b.ready ? (
          <span className="rtag" style={{ background: "var(--success-soft)", color: "var(--success)" }}>
            已配齐，可以发起
          </span>
        ) : (
          <span className="rtag" style={{ background: "var(--warning-soft)", color: "var(--warning)" }}>
            {b.version_no ? `还差 ${new Set(b.missing.map((m) => m.role)).size} 个角色没配齐` : "模板还没发布"}
          </span>
        )}
        {editable && (
          <>
            <Link className="sm" href={`/team/templates/${t.id}`}>
              看模板
            </Link>
            <Popconfirm title="解绑这个模板？" description="之后不能再用它在这个项目里发起" onConfirm={() => void unbind()}>
              <Button size="small" type="text" icon={<Icon name="trash" size={14} />} title="解绑" />
            </Popconfirm>
          </>
        )}
      </div>
      <div className="xs muted" style={{ padding: "8px 16px 0" }}>成员给数字员工下指令、提交和评审；数字员工干活</div>
      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <thead>
          <tr className="xs muted" style={{ textAlign: "left" }}>
            <th style={{ padding: "8px 16px", width: 100 }}>角色</th>
            <th style={{ padding: 8 }}>负责哪几步</th>
            <th style={{ padding: 8, width: 320 }}>成员</th>
            <th style={{ padding: "8px 16px 8px 8px", width: 230 }}>数字员工</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(b.roles_of_template).map(([rk, rn]) => {
            const duty = version.data ? roleDuties(version.data.structure, rk) : { exec: [], admit: [], exit: [] };
            const execs = b.exec_roles.includes(rk);
            const a = b.assignments[rk] ?? { members: [], agent: null };
            const miss = missingOf(rk);
            return (
              <tr key={rk} style={{ borderTop: "1px solid var(--tf-line-soft)", verticalAlign: "top", background: miss.length ? "#fffaf2" : undefined }}>
                <td style={{ padding: "10px 16px" }}>
                  <b>{rn}</b>
                  {!!miss.length && (
                    <div className="xs warn-text">{miss.map((w) => (w === "no_member" ? "缺成员" : w === "no_agent" ? "缺数字员工" : "")).filter(Boolean).join("、")}</div>
                  )}
                </td>
                <td className="sm" style={{ padding: "10px 8px" }}>
                  {!!duty.exec.length && (
                    <div>
                      <span className="rtag exec">做</span> {duty.exec.join("、")}
                    </div>
                  )}
                  {!!duty.admit.length && (
                    <div style={{ marginTop: 3 }}>
                      <span className="rtag admit">准入</span> {duty.admit.join("、")}
                    </div>
                  )}
                  {!!duty.exit.length && (
                    <div style={{ marginTop: 3 }}>
                      <span className="rtag exit">准出</span> {duty.exit.join("、")}
                    </div>
                  )}
                </td>
                <td style={{ padding: "8px" }}>
                  {editable ? (
                    <Select
                      mode="multiple"
                      style={{ width: "100%" }}
                      placeholder="从读写以上的成员里选"
                      value={a.members}
                      status={miss.includes("no_member") ? "warning" : undefined}
                      options={writers.map((m) => ({ value: m.id, label: m.name }))}
                      onChange={(v: string[]) => void assign(rk, { members: v, agent: a.agent })}
                      optionFilterProp="label"
                    />
                  ) : a.members.length ? (
                    <span className="sm">{a.members.map(nameOf).join("、")}</span>
                  ) : (
                    <span className="sm warn-text">未分配</span>
                  )}
                </td>
                <td style={{ padding: "8px 16px 8px 8px" }}>
                  {!execs ? (
                    <span className="faint sm">只评审，不用配</span>
                  ) : editable ? (
                    <Select
                      style={{ width: "100%" }}
                      placeholder="未分配"
                      allowClear
                      value={a.agent ?? undefined}
                      status={miss.includes("no_agent") ? "warning" : undefined}
                      options={agents.filter((x) => x.available).map((x) => ({ value: x.id, label: x.name }))}
                      onChange={(v?: string) => void assign(rk, { members: a.members, agent: v ?? null })}
                    />
                  ) : a.agent ? (
                    <span className="sm">{agents.find((x) => x.id === a.agent)?.name ?? "—"}</span>
                  ) : (
                    <span className="sm warn-text">未分配</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {editable && (
        <div className="xs faint" style={{ padding: "8px 16px 12px" }}>
          成员只能选本团队「{LEVEL.WRITE.label}」以上的人。改了只影响还没开始的步骤
        </div>
      )}
    </div>
  );
}

function BindModal({ project, onClose }: { project: ProjectDetail; onClose: () => void }) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const templates = useTemplates(project.team_id);
  const bound = new Set(project.bindings.map((b) => b.template.id));
  const cand = (templates.data ?? []).filter((t) => t.status === "active" && t.latest_version_no > 0 && !bound.has(t.id));
  const bind = async (tid: string) => {
    try {
      await api.post(`/projects/${project.id}/templates`, { template_id: tid });
      await qc.invalidateQueries({ queryKey: ["project", project.id] });
      onClose();
    } catch (e) {
      message.error(errorText(e));
    }
  };
  return (
    <Modal open title={`给「${project.name}」绑定流程模板`} onCancel={onClose} footer={null} width={560}>
      {templates.isPending ? (
        <Loading />
      ) : cand.length ? (
        <div className="col" style={{ gap: 8 }}>
          <div className="sm muted">本团队已发布的流程模板。绑定后给每个角色分配成员和数字员工</div>
          {cand.map((t) => (
            <div key={t.id} className="radio-card" onClick={() => void bind(t.id)}>
              <Icon name="share" size={16} />
              <div className="grow">
                <b className="sm">{t.name}</b> <span className="xs faint">v{t.latest_version_no}</span>
                {t.description && <div className="xs muted">{t.description}</div>}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <Empty title="本团队已发布的流程模板都绑定了">
          要新的先去 <Link href="/team/templates">团队 → 流程模板</Link> 新建，或从流程市场拷贝
        </Empty>
      )}
    </Modal>
  );
}
