"use client";

/** 团队 · 团队设置：基本信息、通知渠道、归档。只有团队管理员能看 */
import { useQueryClient } from "@tanstack/react-query";
import { App, Button, Input, Popconfirm, Switch } from "antd";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Empty, Icon, Loading, Page, PageHead, Who } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { useTeamChannels, useTeamMembers } from "@/lib/queries";
import { useCan, useCurrentTeam } from "@/lib/team";
import type { Team, TeamChannels } from "@/lib/types";

export default function TeamSettings() {
  const { team, loading } = useCurrentTeam();
  const can = useCan();
  if (loading) return <Loading />;
  if (!team) return null;
  if (!can.configTeam)
    return (
      <Page>
        <div className="card">
          <Empty icon="lock" title="团队设置只有团队管理员能看" />
        </div>
      </Page>
    );
  return <Settings key={team.id} team={team} />;
}

function Settings({ team }: { team: Team }) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const router = useRouter();
  const [name, setName] = useState(team.name);
  const [desc, setDesc] = useState(team.description ?? "");
  const members = useTeamMembers(team.id);
  const channels = useTeamChannels(team.id);
  const [ch, setCh] = useState<TeamChannels | null>(null);
  useEffect(() => setCh(channels.data ?? null), [channels.data]);
  const owners = (members.data?.members ?? []).filter((m) => m.level === "OWNER");

  const save = async () => {
    try {
      await api.patch(`/teams/${team.id}`, { name: name.trim(), description: desc.trim(), version: team.version });
      await qc.invalidateQueries({ queryKey: ["teams"] });
      message.success("已保存");
    } catch (e) {
      message.error(errorText(e));
    }
  };
  const setChannel = async (patch: Partial<TeamChannels>) => {
    const next = { ...(ch ?? { wecom: false, dingtalk: false, email_digest: false }), ...patch };
    setCh(next);
    try {
      await api.put(`/teams/${team.id}/channels`, next);
      await qc.invalidateQueries({ queryKey: ["team", team.id, "channels"] });
    } catch (e) {
      message.error(errorText(e));
    }
  };
  const archive = async () => {
    try {
      await api.post(`/teams/${team.id}/archive`, { archived: true });
      await qc.invalidateQueries({ queryKey: ["teams"] });
      router.push("/projects");
    } catch (e) {
      message.error(errorText(e));
    }
  };

  const sw = (key: keyof TeamChannels, title: string, d: string) => (
    <div className="row" style={{ padding: "8px 0", borderBottom: "1px solid var(--tf-line-soft)" }}>
      <div className="grow">
        <b className="sm">{title}</b>
        <div className="xs muted">{d}</div>
      </div>
      <Switch checked={!!ch?.[key]} onChange={(v) => void setChannel({ [key]: v })} />
    </div>
  );

  return (
    <Page narrow>
      <PageHead title={`团队设置 · ${team.name}`} />
      <div className="card pad" style={{ marginBottom: 16 }}>
        <h2 style={{ marginBottom: 12 }}>基本信息</h2>
        <div className="col" style={{ gap: 12, maxWidth: 560 }}>
          <div>
            <div className="sm" style={{ marginBottom: 6 }}>团队名称</div>
            <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={64} />
            <div className="xs faint" style={{ marginTop: 4 }}>在本租户里唯一</div>
          </div>
          <div>
            <div className="sm" style={{ marginBottom: 6 }}>说明</div>
            <Input.TextArea value={desc} onChange={(e) => setDesc(e.target.value)} maxLength={200} autoSize={{ minRows: 2 }} />
          </div>
          <div className="row">
            <span className="sp" />
            <Button type="primary" disabled={!name.trim()} onClick={() => void save()}>
              保存
            </Button>
          </div>
        </div>
      </div>
      <div className="card pad" style={{ marginBottom: 16 }}>
        <h2 style={{ marginBottom: 10 }}>团队管理员</h2>
        <div className="row" style={{ flexWrap: "wrap", gap: 10 }}>
          {owners.map((m) => (
            <Who key={m.id} id={m.id} name={m.name} size={20} />
          ))}
        </div>
        <div className="xs faint" style={{ marginTop: 8 }}>
          团队管理员 = 这个团队的 Owner（用户中心团队数据权限）：配置团队和流程。在「成员与数字员工」里改权限，至少保留一个
        </div>
      </div>
      <div className="card pad" style={{ marginBottom: 16 }}>
        <h2 style={{ marginBottom: 4 }}>通知</h2>
        <div className="xs muted" style={{ marginBottom: 4 }}>
          评审推给对应的人；每个人在个人设置里绑定自己的账号。渠道要部署时配好凭据才会真的推
        </div>
        {sw("wecom", "企业微信", "团队用企业微信时打开")}
        {sw("dingtalk", "钉钉", "团队用钉钉时打开")}
        {sw("email_digest", "邮件汇总", "每天汇总一次没处理的事")}
      </div>
      <div className="card pad">
        <div className="row">
          <div className="grow">
            <h2>归档团队</h2>
            <div className="sm muted" style={{ marginTop: 4 }}>
              归档后不能再发起流程，进行中的流程继续跑完；成员权限仍在用户中心
            </div>
          </div>
          <Popconfirm title={`归档「${team.name}」？`} onConfirm={() => void archive()}>
            <Button danger icon={<Icon name="archive" size={14} />}>
              归档
            </Button>
          </Popconfirm>
        </div>
      </div>
    </Page>
  );
}
