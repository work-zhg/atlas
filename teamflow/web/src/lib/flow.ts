"use client";

/**
 * 流程各页共用：流程详情、事件、人名、数字员工名，并订阅实时事件（有变化就刷新）。
 */
import { useNameOf } from "@/components/ui";

import { useEvents, useProcess, useTeamAgents } from "./queries";
import { useProcessStream } from "./stream";
import { useFollowTeam } from "./team";

export function useFlow(id: string | undefined) {
  const proc = useProcess(id);
  const events = useEvents(id);
  const agents = useTeamAgents(proc.data?.team_id);
  useFollowTeam(proc.data?.team_id);
  const last = events.data ? (events.data.length ? events.data[events.data.length - 1]!.seq : 0) : undefined;
  useProcessStream(id, last);

  const p = proc.data;
  const ids = [
    p?.started_by,
    ...Object.values(p?.roles ?? {}).flat(),
    ...(events.data ?? []).map((e) => e.actor),
    ...(p?.nodes ?? []).flatMap((n) => [...(n.open_review?.reviewers ?? []), n.latest_submission?.submitted_by]),
  ];
  const nameOf = useNameOf(ids);
  const byAtlasId = Object.fromEntries((agents.data ?? []).map((a) => [a.atlas_agent_id, a.name]));
  const agentName = (atlasId: string | null | undefined) => (atlasId ? (byAtlasId[atlasId] ?? "数字员工") : null);
  const nodeName = (nid: string | null | undefined) => (nid ? (p?.structure.nodes[nid]?.name ?? nid) : "");
  /** 发起时锁定的模板版本（在 process.started 事件里） */
  const version = events.data?.find((e) => e.type === "process.started")?.data.version as number | undefined;
  return { proc, events, agents, nameOf, agentName, nodeName, version };
}
