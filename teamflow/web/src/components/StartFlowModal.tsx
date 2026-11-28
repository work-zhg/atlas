"use client";

/**
 * 发起流程：只选模板、写标题。角色和数字员工按项目配置；没配齐的模板不能选。
 * 发起时后端先向 Agent 平台申请流程空间，再建流程；步骤都未开始，第一步由负责人传资料后点「开始」。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Input, Modal } from "antd";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { api, errorText } from "@/lib/api";
import { summary } from "@/lib/structure";
import type { Binding, Process, ProjectDetail, Structure } from "@/lib/types";

import { useNameOf } from "./ui";

const MISSING = { no_member: "没有成员", no_agent: "没有数字员工", not_published: "模板还没发布" } as Record<string, string>;

export function StartFlowModal({ project, onClose }: { project: ProjectDetail; onClose: () => void }) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const router = useRouter();
  const ready = project.bindings.filter((b) => b.ready && b.version_no > 0);
  const [tpl, setTpl] = useState<string | undefined>(ready[0]?.template.id);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const picked = project.bindings.find((b) => b.template.id === tpl);
  const nameOf = useNameOf(Object.values(picked?.assignments ?? {}).flatMap((a) => a.members));
  const version = useQuery({
    queryKey: ["template", tpl, "version", picked?.version_no],
    queryFn: () => api.get<{ structure: Structure }>(`/templates/${tpl}/versions/${picked!.version_no}`),
    enabled: !!picked && picked.version_no > 0,
    staleTime: Infinity,
  });

  // 第一步的负责人：模板第一步的执行角色，现在分配给了谁
  const first = version.data?.structure;
  const firstRoles = first ? first.flow.slice(0, 1).flatMap((st) => (st.type === "node" ? [st.id] : st.tracks.map((t) => t.nodes[0]!))).map((n) => first.nodes[n]?.exec_role ?? "") : [];
  const firstOwners = [...new Set(firstRoles.flatMap((r) => picked?.assignments[r]?.members ?? []))].map(nameOf).join("、");

  const go = async () => {
    setBusy(true);
    try {
      const proc = await api.post<Process>(`/projects/${project.id}/processes`, { template_id: tpl, title: title.trim() });
      await qc.invalidateQueries({ queryKey: ["project", project.id] });
      onClose();
      router.push(`/flows/${proc.id}`);
    } catch (e) {
      message.error(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const card = (b: Binding) => {
    const off = !b.ready || b.version_no === 0;
    return (
      <div
        key={b.template.id}
        className={`radio-card ${b.template.id === tpl ? "on" : ""} ${off ? "off" : ""}`}
        onClick={() => !off && setTpl(b.template.id)}
      >
        <input type="radio" readOnly checked={b.template.id === tpl} disabled={off} style={{ marginTop: 3 }} />
        <div className="grow">
          <div className="row">
            <b className="sm">{b.template.name}</b>
            <span className="xs faint">{b.version_no ? `v${b.version_no}` : "还没发布"}</span>
          </div>
          {b.template.id === tpl && version.data && <div className="mini">{summary(version.data.structure)}</div>}
          {!b.ready && (
            <div className="xs warn-text" style={{ marginTop: 6 }}>
              还差：{b.missing.map((m) => `${b.roles_of_template[m.role] ?? m.role}${MISSING[m.what] ?? ""}`).join("、")}，配齐才能发起 ·{" "}
              <Link href={`/projects/${project.id}/config`} onClick={onClose}>
                去项目配置
              </Link>
            </div>
          )}
        </div>
      </div>
    );
  };

  return (
    <Modal
      open
      width={620}
      title={`发起流程 · ${project.name}`}
      onCancel={onClose}
      onOk={go}
      okText="发起"
      confirmLoading={busy}
      okButtonProps={{ disabled: !tpl || !title.trim() }}
    >
      <div className="sm" style={{ margin: "4px 0 6px", fontWeight: 600 }}>
        流程模板
      </div>
      <div className="col" style={{ gap: 8, marginBottom: 14 }}>
        {project.bindings.length ? project.bindings.map(card) : <div className="sm muted">项目还没绑定流程模板</div>}
      </div>
      <div className="sm" style={{ marginBottom: 6, fontWeight: 600 }}>
        标题
      </div>
      <Input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={120} placeholder="一句话说清要做什么（数字员工也会看到）" />
      {firstOwners && (
        <div className="xs muted" style={{ marginTop: 10 }}>
          发起后先上传资料，再由 {firstOwners} 开始第一步
        </div>
      )}
    </Modal>
  );
}
