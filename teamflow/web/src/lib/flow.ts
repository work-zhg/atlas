"use client";

/**
 * 流程各页共用：流程详情、事件、人名、数字员工名，并订阅实时事件（有变化就刷新）；往流程里传文件。
 */
import { useQueryClient } from "@tanstack/react-query";
import { App } from "antd";

import { useNameOf } from "@/components/ui";

import { errorText, putFile } from "./api";
import { useEvents, useProcess, useProject, useTeamAgents } from "./queries";
import { useProcessStream } from "./stream";
import { useFollowTeam } from "./team";
import type { NodeRow } from "./types";

export function useFlow(id: string | undefined) {
  const proc = useProcess(id);
  const events = useEvents(id);
  const agents = useTeamAgents(proc.data?.team_id);
  const project = useProject(proc.data?.project_id);
  useFollowTeam(proc.data?.team_id);
  const last = events.data ? (events.data.length ? events.data[events.data.length - 1]!.seq : 0) : undefined;
  useProcessStream(id, last);

  const p = proc.data;
  const ids = [
    p?.started_by,
    ...Object.values(p?.roles ?? {}).flat(),
    ...(events.data ?? []).map((e) => e.actor),
    ...(p?.nodes ?? []).flatMap((n) => [...(n.open_review?.reviewers ?? []), n.latest_submission?.submitted_by, n.session.taken_by]),
  ];
  const nameOf = useNameOf(ids);
  const byAtlasId = Object.fromEntries((agents.data ?? []).map((a) => [a.atlas_agent_id, a.name]));
  const agentName = (atlasId: string | null | undefined) => (atlasId ? (byAtlasId[atlasId] ?? "数字员工") : null);
  const nodeName = (nid: string | null | undefined) => (nid ? (p?.structure.nodes[nid]?.name ?? nid) : "");
  /** 这一步的数字员工：开始后是锁定的那个；还没开始的按项目配置里这个角色现在分配的 */
  const assignments = project.data?.bindings.find((b) => b.template.id === p?.template_id)?.assignments;
  const agentOf = (n: Pick<NodeRow, "atlas_agent_id" | "exec_role">) => {
    if (n.atlas_agent_id) return agentName(n.atlas_agent_id);
    const planned = assignments?.[n.exec_role]?.agent;
    return planned ? (agents.data?.find((a) => a.id === planned)?.name ?? null) : null;
  };
  /** 发起时用的模板版本（在 process.started 事件里） */
  const version = events.data?.find((e) => e.type === "process.started")?.data.version as number | undefined;
  return { proc, events, agents, project, nameOf, agentName, agentOf, nodeName, version };
}

/** 往流程里传文件（放进 dir 文件夹）：传完刷新文件列表和事件 */
export function useUpload(processId: string) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  return async (file: File, dir: string) => {
    const path = dir ? `${dir}/${file.name}` : file.name;
    try {
      await putFile(`/processes/${processId}/space`, file, { path });
      message.success(`已上传 ${path}`);
      await qc.invalidateQueries({ queryKey: ["process", processId, "space"] });
      return true;
    } catch (e) {
      message.error(`${file.name}：${errorText(e)}`);
      return false;
    }
  };
}
