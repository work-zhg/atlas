"use client";

/** 团队 · 流程模板：归团队；从流程市场拷贝或空白新建，项目绑定后才能发起。只有团队管理员能看 */
import { useQueryClient } from "@tanstack/react-query";
import { App, Button, Input, Modal, Tag } from "antd";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Empty, Icon, Loading, Page, PageHead } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { ago } from "@/lib/format";
import { useTemplates } from "@/lib/queries";
import { useCan, useCurrentTeam } from "@/lib/team";
import type { Template } from "@/lib/types";

export default function TemplatesPage() {
  const { team, loading } = useCurrentTeam();
  const can = useCan();
  const router = useRouter();
  const { message } = App.useApp();
  const qc = useQueryClient();
  const list = useTemplates(team?.id);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");

  if (loading) return <Loading />;
  if (!team) return null;
  if (!can.configFlow)
    return (
      <Page>
        <div className="card">
          <Empty icon="lock" title="流程模板只有团队管理员能看" />
        </div>
      </Page>
    );

  const create = async () => {
    try {
      const t = await api.post<Template>(`/teams/${team.id}/templates`, { name: name.trim() });
      await qc.invalidateQueries({ queryKey: ["team", team.id, "templates"] });
      router.push(`/team/templates/${t.id}`);
    } catch (e) {
      message.error(errorText(e));
    }
  };

  const rows = (list.data ?? []).filter((t) => t.status === "active");
  return (
    <Page>
      <PageHead
        title="流程模板"
        sub={`${team.name} 的流程模板：项目绑定后才能发起。节点交什么由执行人提交评审时从流程空间选，格式由数字员工的技能约定`}
        extra={
          <>
            <Button icon={<Icon name="grid" size={14} />} onClick={() => router.push("/market")}>
              去流程市场拷贝
            </Button>
            <Button type="primary" icon={<Icon name="plus" size={14} />} onClick={() => setCreating(true)}>
              空白新建
            </Button>
          </>
        }
      />
      {list.isPending ? (
        <Loading />
      ) : !rows.length ? (
        <div className="card">
          <Empty icon="list" title="还没有流程模板">从流程市场拷贝一个改改，或者空白新建</Empty>
        </div>
      ) : (
        <div className="cgrid">
          {rows.map((t) => (
            <Link key={t.id} className="tcard" href={`/team/templates/${t.id}`}>
              <div className="row">
                <span className="ic2" style={{ background: "var(--primary)" }}>
                  <Icon name="share" size={16} />
                </span>
                <div className="grow">
                  <b>{t.name}</b>
                  <div className="xs faint">{t.latest_version_no ? `v${t.latest_version_no} · 更新于 ${ago(t.updated_at)}` : "草稿，还没发布"}</div>
                </div>
              </div>
              {t.description && <div className="sm muted" style={{ marginTop: 8 }}>{t.description}</div>}
              <div className="row" style={{ marginTop: 10, flexWrap: "wrap" }}>
                {t.origin ? <Tag icon={<Icon name="grid" size={11} />}>拷贝自流程市场 v{t.origin.version}</Tag> : <Tag>自建</Tag>}
              </div>
            </Link>
          ))}
        </div>
      )}
      <Modal open={creating} title="空白新建流程模板" onCancel={() => setCreating(false)} onOk={() => void create()} okText="新建" okButtonProps={{ disabled: !name.trim() }}>
        <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={64} placeholder="模板名称，团队内唯一" />
        <div className="xs faint" style={{ marginTop: 6 }}>从一个节点开始编排；发布后才能被项目绑定</div>
      </Modal>
    </Page>
  );
}
