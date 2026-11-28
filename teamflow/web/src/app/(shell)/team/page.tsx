"use client";

/**
 * 团队 · 成员与数字员工。
 *
 * ★ 数字员工只引用 Agent 平台里以数字员工为权限的智能体（TeamFlow 不创建）；接入人必须是它的 Owner，
 *   之后由接入人的身份给流程里的人授使用权。技能、知识库、模型在 Agent 平台里配。
 * ★ 团队权限存在用户中心的团队数据权限里：读写 = 发起、执行、评审；只读 = 只能看；团队管理员 = Owner。
 */
import { useQueryClient } from "@tanstack/react-query";
import { App, Button, Input, Modal, Popconfirm, Select, Spin, Table, Tag } from "antd";
import { useState } from "react";

import { AgentChip, Avatar, Empty, Icon, LoadError, Loading, Page, PageHead } from "@/components/ui";
import { api, ATLAS_UI_BASE, errorText } from "@/lib/api";
import { LEVEL } from "@/lib/format";
import { useAgentCandidates, useAgentCard, useTeamAgents, useTeamMembers } from "@/lib/queries";
import { useCan, useCurrentTeam } from "@/lib/team";
import type { Level, Team, TeamAgent, TeamGrant } from "@/lib/types";

export default function TeamPage() {
  const { team, loading } = useCurrentTeam();
  const can = useCan();
  const agents = useTeamAgents(team?.id);
  const members = useTeamMembers(team?.id);
  const [adding, setAdding] = useState<"member" | "agent" | null>(null);

  if (loading) return <Loading />;
  if (!team) return <Page><div className="card"><Empty icon="users" title="你还不在任何团队里" /></div></Page>;
  const adm = can.configTeam;

  return (
    <Page>
      <PageHead
        title={`成员与数字员工 · ${team.name}`}
        extra={
          adm && (
            <>
              <Button icon={<Icon name="plus" size={14} />} onClick={() => setAdding("member")}>
                添加成员
              </Button>
              <Button type="primary" icon={<Icon name="bot" size={14} />} onClick={() => setAdding("agent")}>
                接入数字员工
              </Button>
            </>
          )
        }
      />
      {!adm && (
        <div className="banner info" style={{ marginBottom: 14 }}>
          <Icon name="lock" size={15} />
          <div>加人、改权限、接入数字员工只有团队管理员能做；你在这个团队是{LEVEL[team.my_level ?? "NONE"].label}成员</div>
        </div>
      )}
      <h2 style={{ marginBottom: 10 }}>数字员工</h2>
      {agents.error ? (
        <LoadError error={agents.error} />
      ) : agents.data && !agents.data.length ? (
        <div className="card">
          <Empty icon="bot" title="还没接入数字员工">
            {adm ? "点右上角「接入数字员工」，从你在 Atlas 里建的数字员工中选" : "等团队管理员接入"}
          </Empty>
        </div>
      ) : (
        <div className="cgrid">
          {(agents.data ?? []).map((a) => (
            <AgentCardView key={a.id} team={team} agent={a} admin={adm} />
          ))}
        </div>
      )}
      <div className="sec">
        <h2>
          成员
        </h2>
        {members.error ? (
          <LoadError error={members.error} />
        ) : !members.data ? (
          <Spin />
        ) : (
          <Members team={team} grants={members.data.grants} people={members.data.members} admin={adm} />
        )}
        <div className="xs faint" style={{ marginTop: 8 }}>
          读写：发起流程、在项目里担任角色 · 只读：只能看 · 团队管理员：另外能配置团队、流程模板、项目
        </div>
      </div>
      {adding === "member" && <AddMembers team={team} onClose={() => setAdding(null)} />}
      {adding === "agent" && <AddAgent team={team} onClose={() => setAdding(null)} />}
    </Page>
  );
}

function AgentCardView({ team, agent: a, admin }: { team: Team; agent: TeamAgent; admin: boolean }) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const card = useAgentCard(team.id, a.id);
  const remove = async () => {
    try {
      await api.del(`/teams/${team.id}/agents/${a.id}`);
      await qc.invalidateQueries({ queryKey: ["team", team.id, "agents"] });
    } catch (e) {
      message.error(errorText(e));
    }
  };
  return (
    <div className="tcard" style={{ cursor: "default" }}>
      <div className="row">
        <Avatar name={a.name} size={38} bot />
        <div className="grow">
          <b>{a.name}</b>
        </div>
        {!a.available ? <Tag color="red">不可用</Tag> : a.stats.working ? <Tag color="blue">干活中 {a.stats.working}</Tag> : <Tag>空闲</Tag>}
      </div>
      {!!card.data?.card?.skills.length && (
        <div className="row" style={{ gap: 6, marginTop: 10, flexWrap: "wrap" }}>
          <span className="xs faint">技能</span>
          {card.data.card.skills.map((s) => (
            <span key={s} className="rtag">
              {s}
            </span>
          ))}
        </div>
      )}
      {a.description && <div className="xs muted" style={{ marginTop: 8 }}>{a.description}</div>}
      <div className="row xs muted" style={{ marginTop: 10, paddingTop: 10, borderTop: "1px solid var(--tf-line-soft)", whiteSpace: "nowrap" }}>
        <span>
          完成 <b style={{ color: "var(--fg)" }}>{a.stats.passed}</b> 步
        </span>
        {a.stats.avg_rounds != null && (
          <span>
            平均 <b style={{ color: "var(--fg)" }}>{a.stats.avg_rounds.toFixed(1)}</b> 轮
          </span>
        )}
        <span className="sp" />
        {admin && (
          <>
            <a href={`${ATLAS_UI_BASE}/agents/${a.atlas_agent_id}`} target="_blank" rel="noreferrer">
              去 Atlas 编辑
            </a>
            <Popconfirm title={`从团队移除「${a.name}」？`} description="项目配置里用它的角色要换一个" onConfirm={() => void remove()}>
              <a style={{ color: "var(--danger)" }}>移除</a>
            </Popconfirm>
          </>
        )}
      </div>
    </div>
  );
}

function Members({ team, grants, people, admin }: { team: Team; grants: TeamGrant[]; people: { id: string; name: string; account: string; level: Level }[]; admin: boolean }) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const reload = () => qc.invalidateQueries({ queryKey: ["team", team.id, "members"] });
  const setLevel = async (g: TeamGrant, level: Level) => {
    try {
      await api.patch(`/teams/${team.id}/members/${g.id}`, { level });
      await reload();
    } catch (e) {
      message.error(errorText(e));
    }
  };
  const remove = async (g: TeamGrant) => {
    try {
      await api.del(`/teams/${team.id}/members/${g.id}`);
      await reload();
    } catch (e) {
      message.error(errorText(e));
    }
  };
  // 按人的授权逐个列（部门授权另起一行），只读的人看到的是成员清单
  const rows = admin ? grants : null;
  if (!rows)
    return (
      <Table
        rowKey="id"
        size="middle"
        pagination={false}
        dataSource={people}
        columns={[
          { title: "成员", render: (_, m) => <span className="who"><Avatar id={m.id} name={m.name} size={22} />{m.name}</span> },
          { title: "账号", dataIndex: "account", render: (v: string) => <span className="sm muted">{v}</span> },
          { title: "团队权限", width: 160, render: (_, m) => <Tag color={LEVEL[m.level].color}>{LEVEL[m.level].label}</Tag> },
        ]}
      />
    );
  return (
    <Table
      rowKey="id"
      size="middle"
      pagination={false}
      dataSource={rows}
      columns={[
        {
          title: "成员",
          render: (_, g) => (
            <span className="who">
              {g.subject.type === "DEPT" ? <Icon name="users" size={16} /> : <Avatar id={g.subject.id} name={g.subject.name ?? "?"} size={22} />}
              {g.subject.name ?? g.subject.id}
              {g.subject.type === "DEPT" && <span className="xs faint">（部门{g.include_sub ? "，含下级" : ""}）</span>}
            </span>
          ),
        },
        {
          title: "团队权限",
          width: 200,
          render: (_, g) => (
            <Select
              size="small"
              style={{ width: 150 }}
              value={g.permission}
              onChange={(v: Level) => void setLevel(g, v)}
              options={(["OWNER", "WRITE", "READ"] as Level[]).map((l) => ({ value: l, label: LEVEL[l].label }))}
            />
          ),
        },
        {
          title: "",
          width: 60,
          render: (_, g) => (
            <Popconfirm title="移出团队？" description="至少保留一个团队管理员" onConfirm={() => void remove(g)}>
              <Button size="small" type="text" icon={<Icon name="trash" size={14} />} />
            </Popconfirm>
          ),
        },
      ]}
    />
  );
}

function AddMembers({ team, onClose }: { team: Team; onClose: () => void }) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [found, setFound] = useState<{ id: string; name: string; account?: string }[]>([]);
  const [picked, setPicked] = useState<string[]>([]);
  const [level, setLevel] = useState<Level>("WRITE");
  const [busy, setBusy] = useState(false);
  const search = async (text: string) => {
    setQ(text);
    try {
      setFound(await api.get(`/people`, { q: text }));
    } catch {
      setFound([]);
    }
  };
  const ok = async () => {
    setBusy(true);
    try {
      await api.post(`/teams/${team.id}/members`, { subjects: picked.map((id) => ({ type: "USER", id })), level });
      await qc.invalidateQueries({ queryKey: ["team", team.id, "members"] });
      onClose();
    } catch (e) {
      message.error(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open title={`添加成员 · ${team.name}`} onCancel={onClose} onOk={() => void ok()} okText="添加" confirmLoading={busy} okButtonProps={{ disabled: !picked.length }}>
      <div className="col" style={{ gap: 12 }}>
        <Select
          mode="multiple"
          showSearch
          filterOption={false}
          placeholder="搜本租户的人：姓名或账号"
          searchValue={q}
          onSearch={(v) => void search(v)}
          onFocus={() => !found.length && void search("")}
          value={picked}
          onChange={setPicked}
          options={found.map((u) => ({ value: u.id, label: `${u.name}${u.account ? `（${u.account}）` : ""}` }))}
          style={{ width: "100%" }}
        />
        <div className="row">
          <span className="sm">团队权限</span>
          <Select
            style={{ width: 160 }}
            value={level}
            onChange={setLevel}
            options={(["WRITE", "READ", "OWNER"] as Level[]).map((l) => ({ value: l, label: LEVEL[l].label }))}
          />
        </div>
        <div className="xs faint">只读的人不能在项目里担任角色</div>
      </div>
    </Modal>
  );
}

function AddAgent({ team, onClose }: { team: Team; onClose: () => void }) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const cand = useAgentCandidates(team.id, q, true);
  const add = async (id: string) => {
    try {
      await api.post(`/teams/${team.id}/agents`, { atlas_agent_id: id });
      await qc.invalidateQueries({ queryKey: ["team", team.id, "agents"] });
      await qc.invalidateQueries({ queryKey: ["team", team.id, "candidates"] });
      message.success("已接入");
    } catch (e) {
      message.error(errorText(e));
    }
  };
  return (
    <Modal open title={`接入数字员工 · ${team.name}`} onCancel={onClose} footer={<Button onClick={onClose}>完成</Button>} width={600}>
      <div className="sm muted" style={{ marginBottom: 10 }}>
        从你在 Atlas 里建的数字员工中选。它会什么（技能、知识库、模型）在 Atlas 里配
      </div>
      <Input.Search placeholder="搜智能体" allowClear onSearch={setQ} style={{ marginBottom: 10 }} />
      {cand.isPending ? (
        <Spin />
      ) : cand.error ? (
        <LoadError error={cand.error} />
      ) : !cand.data?.length ? (
        <div className="sm muted">没有可接入的：先在 Atlas 里建一个数字员工</div>
      ) : (
        <div className="col" style={{ gap: 8, maxHeight: 380, overflowY: "auto" }}>
          {cand.data.map((c) => (
            <div key={c.atlas_agent_id} className="pickrow" style={{ cursor: "default" }}>
              <AgentChip name={c.name} size={22} />
              <span className="xs faint grow ellipsis">{c.description}</span>
              {c.added ? (
                <Tag>已接入</Tag>
              ) : (
                <Button size="small" type="primary" onClick={() => void add(c.atlas_agent_id)}>
                  接入
                </Button>
              )}
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}
