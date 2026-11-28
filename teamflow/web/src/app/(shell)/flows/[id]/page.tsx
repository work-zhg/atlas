"use client";

/**
 * 流程详情：流程图（状态、轮次、执行的数字员工、准入准出由谁审）、产出物、最近事件、角色分配。
 *
 * ★ 发起后节点都待开始：首节点的执行人把资料传进流程空间后点「开始」，数字员工才开工。
 */
import { useQueryClient } from "@tanstack/react-query";
import { App, Button, Dropdown, Input, Modal } from "antd";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";

import { FlowGraph, Legend, type NodeState } from "@/components/FlowGraph";
import { AgentChip, Crumb, FileChip, Icon, LoadError, Loading, Page, PageHead, ProcessStatusTag, Who } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { describe } from "@/lib/events";
import { ago, dt } from "@/lib/format";
import { useFlow } from "@/lib/flow";
import { useProject } from "@/lib/queries";
import { isRoot, nodeIds } from "@/lib/structure";
import { useCan } from "@/lib/team";

export default function FlowDetail() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { message } = App.useApp();
  const qc = useQueryClient();
  const { proc, events, agents, nameOf, agentName, nodeName, version } = useFlow(id);
  const project = useProject(proc.data?.project_id);
  const can = useCan();
  const [terminating, setTerminating] = useState(false);
  const [reason, setReason] = useState("");

  if (proc.error) return <LoadError error={proc.error} onRetry={() => void proc.refetch()} />;
  if (!proc.data) return <Loading />;
  const p = proc.data;
  const s = p.structure;
  const byId = Object.fromEntries(p.nodes.map((n) => [n.id, n]));
  const states: Record<string, NodeState> = Object.fromEntries(
    p.nodes.map((n) => [n.id, { status: n.status, round: n.round, agent: agentName(n.atlas_agent_id) }]),
  );
  const binding = project.data?.bindings.find((b) => b.template.id === p.template_id);
  /** 角色当前分配的数字员工（项目配置里存的是团队接入的数字员工 id） */
  const agentOfRole = (role: string) => {
    const a = binding?.assignments[role]?.agent;
    return a ? agents.data?.find((x) => x.id === a)?.name : undefined;
  };
  const roots = nodeIds(s).filter((n) => isRoot(s, n));
  const waitingRoots = roots.filter((n) => byId[n]?.status === "pending");
  const subs = nodeIds(s).filter((n) => byId[n]?.latest_submission);

  const begin = async (nid: string) => {
    try {
      await api.post(`/processes/${p.id}/nodes/${nid}/begin`);
      await qc.invalidateQueries({ queryKey: ["process", p.id] });
      router.push(`/flows/${p.id}/n/${nid}`);
    } catch (e) {
      message.error(errorText(e));
    }
  };
  const terminate = async () => {
    try {
      await api.post(`/processes/${p.id}/terminate`, { reason: reason.trim() || null });
      await qc.invalidateQueries({ queryKey: ["process", p.id] });
      setTerminating(false);
    } catch (e) {
      message.error(errorText(e));
    }
  };

  return (
    <Page>
      <Crumb
        items={[
          [project.data?.name ?? "项目", `/projects/${p.project_id}`],
          ["流程", `/projects/${p.project_id}`],
          [p.code, undefined],
        ]}
      />
      <PageHead
        title={
          <>
            {p.title} <ProcessStatusTag status={p.status} />
          </>
        }
        sub={
          <>
            <span>
              {binding?.template.name ?? "流程模板"} <b style={{ color: "var(--fg-2)" }}>{version ? `v${version}` : ""}</b>
            </span>
            <span className="lock" title="模板之后发布的新版本不影响这条流程">
              <Icon name="lock" size={12} />
              发起时锁定
            </span>
            <span>·</span>
            <Link className="lock" href={`/flows/${p.id}/space`} title="向 Agent 平台申请的流程空间：这条流程所有节点的数字员工会话都挂载它">
              <Icon name="folder" size={12} />
              流程空间
            </Link>
            <span>·</span>
            <Who id={p.started_by} name={nameOf(p.started_by)} size={16} /> {dt(p.started_at)} 发起
          </>
        }
        extra={
          <>
            <Button icon={<Icon name="folder" size={14} />} onClick={() => router.push(`/flows/${p.id}/space`)}>
              流程空间
            </Button>
            <Button icon={<Icon name="clock" size={14} />} onClick={() => router.push(`/flows/${p.id}/trace`)}>
              追溯
            </Button>
            {p.can_terminate && (
              <Dropdown
                trigger={["click"]}
                menu={{ items: [{ key: "t", danger: true, label: "终止流程" }], onClick: () => setTerminating(true) }}
              >
                <Button icon={<Icon name="more" size={14} />} />
              </Dropdown>
            )}
          </>
        }
      />

      {p.status === "running" && waitingRoots.length > 0 && (
        <div className="banner info" style={{ marginBottom: 14 }}>
          <Icon name="play" size={15} />
          <div className="grow">
            <b>待开始</b>：先把参考资料上传到 <Link href={`/flows/${p.id}/space`}>流程空间</Link>，再点「开始」：数字员工收到工作说明后开工
            {waitingRoots.map((n) => {
              const node = byId[n]!;
              return (
                <div key={n} className="row" style={{ marginTop: 8 }}>
                  <b className="sm">{node.name}</b>
                  <span className="xs muted">执行：{node.executors.map(nameOf).join("、") || "—"}</span>
                  <span className="sp" />
                  {node.i_am_executor ? (
                    <Button size="small" type="primary" onClick={() => void begin(n)}>
                      开始
                    </Button>
                  ) : (
                    <span className="xs faint">等执行人开始</span>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      <div className="card">
        <FlowGraph structure={s} states={states} href={(nid) => `/flows/${p.id}/n/${nid}`} />
        <Legend />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,1fr)", gap: 16, marginTop: 16 }}>
        <div className="card pad">
          <div className="row" style={{ marginBottom: 8 }}>
            <h2>产出物</h2>
            <span className="xs faint">各节点提交评审时从流程空间选的文件</span>
            <span className="sp" />
            <Link className="sm" href={`/flows/${p.id}/space`}>
              打开流程空间
            </Link>
          </div>
          {subs.length ? (
            subs.map((n) => {
              const sub = byId[n]!.latest_submission!;
              return (
                <div key={n} className="row" style={{ padding: "6px 0", borderBottom: "1px solid var(--tf-line-soft)", alignItems: "flex-start" }}>
                  <b className="sm" style={{ width: 84, flex: "none", marginTop: 5 }}>
                    {byId[n]!.name}
                  </b>
                  <span className="grow">
                    {sub.paths.map((x) => (
                      <FileChip key={x} path={x} />
                    ))}
                  </span>
                  <span className="xs faint" style={{ marginTop: 6, whiteSpace: "nowrap" }}>
                    第 {sub.round} 轮提交
                  </span>
                </div>
              );
            })
          ) : (
            <div className="sm muted">还没有节点提交过产出物</div>
          )}
        </div>
        <div className="card pad">
          <div className="row" style={{ marginBottom: 8 }}>
            <h2>最近</h2>
            <span className="sp" />
            <Link className="sm" href={`/flows/${p.id}/trace`}>
              完整追溯
            </Link>
          </div>
          {(events.data ?? [])
            .slice(-6)
            .reverse()
            .map((e) => (
              <div key={e.seq} className="row" style={{ padding: "5px 0", alignItems: "flex-start" }}>
                <span className="xs faint" style={{ width: 76, flex: "none" }}>
                  {ago(e.at)}
                </span>
                <span className="sm">{describe(e, nameOf, nodeName).text}</span>
              </div>
            ))}
        </div>
      </div>

      <div className="card pad" style={{ marginTop: 16 }}>
        <div className="row" style={{ marginBottom: 6 }}>
          <h2>角色分配</h2>
          <span className="xs faint">来自项目配置；改分配只影响还没开工的节点，已开工节点的数字员工已锁定</span>
          <span className="sp" />
          {can.configFlow && (
            <Link className="sm" href={`/projects/${p.project_id}/config`}>
              去项目配置改
            </Link>
          )}
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(2,minmax(0,1fr))", columnGap: 24 }}>
          {Object.entries(s.roles).map(([rk, rn]) => {
            const agent = agentOfRole(rk);
            return (
              <div key={rk} className="row" style={{ padding: "7px 0", borderBottom: "1px solid var(--tf-line-soft)" }}>
                <b className="sm" style={{ width: 84 }}>
                  {rn}
                </b>
                <span className="grow row" style={{ flexWrap: "wrap" }}>
                  {(p.roles[rk] ?? []).map((u) => (
                    <Who key={u} id={u} name={nameOf(u)} size={18} />
                  ))}
                </span>
                {agent && <AgentChip name={agent} size={18} />}
              </div>
            );
          })}
        </div>
      </div>

      <Modal open={terminating} title="终止流程" onCancel={() => setTerminating(false)} onOk={() => void terminate()} okText="终止" okButtonProps={{ danger: true }}>
        <div className="sm muted" style={{ marginBottom: 8 }}>
          进行中的数字员工会话停下，未完成的评审取消；流程空间转只读保留
        </div>
        <Input.TextArea value={reason} onChange={(e) => setReason(e.target.value)} placeholder="原因（可选）" maxLength={200} autoSize={{ minRows: 2 }} />
      </Modal>
    </Page>
  );
}
