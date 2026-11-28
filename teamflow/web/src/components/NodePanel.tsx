"use client";

/**
 * 节点页右侧：评审（轮到我评审时）或 产出物 / 输入 / 流程空间 / 本节点 / 评审记录。
 *
 * ★ 准出评审：审本节点这一轮提交的文件 → 同意 / 驳回修改（本节点再做一轮）。
 * ★ 准入评审：审上游提交的文件 → 准入通过（本节点开工）/ 打回上游（上游重做）。驳回、打回必须写意见，
 *   意见就是下一轮的指令。
 */
import { useQueryClient } from "@tanstack/react-query";
import { App, Button, Input, Select, Tabs } from "antd";
import Link from "next/link";
import { useState } from "react";

import { api, errorText } from "@/lib/api";
import { dt, RULE, STAGE } from "@/lib/format";
import { useAgentCard, useSpace, useTeamAgents } from "@/lib/queries";
import { roleNames, upstreams } from "@/lib/structure";
import type { NodeDetail, ProcessDetail, ProcessNode, Review } from "@/lib/types";

import { FilePreview } from "./FilePreview";
import { AgentChip, FileChip, Icon, NodeStatusTag, Who } from "./ui";

interface Props {
  process: ProcessDetail;
  node: ProcessNode;
  detail: NodeDetail | undefined;
  nameOf: (id: string | null | undefined) => string;
  agentName: (atlasId: string | null | undefined) => string | null;
}

export function NodePanel(p: Props) {
  const open = p.node.open_review;
  if (open?.i_can_vote) return <Decide {...p} stage={open.stage} />;
  return <Tabbed {...p} />;
}

function Decide({ process, node, detail, nameOf, stage }: Props & { stage: "admit" | "exit" }) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const s = process.structure;
  const spec = s.nodes[node.id]!;
  const review = detail?.reviews.find((r) => r.status === "open" && r.stage === stage);
  const ups = (upstreams(s)[node.id] ?? []).map((u) => process.nodes.find((n) => n.id === u)!).filter(Boolean);
  const files = stage === "exit" ? (node.latest_submission?.paths ?? []) : ups.flatMap((u) => u.latest_submission?.paths ?? []);
  const [preview, setPreview] = useState<string | undefined>(files[0]);
  const [decision, setDecision] = useState<"approve" | "reject" | null>(null);
  const [comment, setComment] = useState("");
  const [returnTo, setReturnTo] = useState<string | undefined>(ups[0]?.id);
  const [busy, setBusy] = useState(false);

  const go = async () => {
    setBusy(true);
    try {
      await api.post(`/processes/${process.id}/nodes/${node.id}/votes`, {
        decision,
        comment: comment.trim() || null,
        return_to: stage === "admit" && decision === "reject" ? returnTo : null,
      });
      await qc.invalidateQueries({ queryKey: ["process", process.id] });
      await qc.invalidateQueries({ queryKey: ["node", process.id, node.id] });
      await qc.invalidateQueries({ queryKey: ["todos"] });
      message.success("结论已提交");
    } catch (e) {
      message.error(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const opts =
    stage === "exit"
      ? ([
          ["approve", "同意", "全部同意就准出", "var(--success)"],
          ["reject", "驳回修改", "本节点再做一轮", "var(--danger)"],
        ] as const)
      : ([
          ["approve", "准入通过", "本节点开工", "var(--success)"],
          ["reject", "打回上游", "上游重做一轮", "var(--danger)"],
        ] as const);
  const needWhy = decision === "reject" && !comment.trim();

  return (
    <div className="rp">
      <div className={`banner ${stage === "exit" ? "warn" : "purple"}`}>
        <Icon name={stage === "exit" ? "shield" : "check"} size={15} />
        <div>
          <b>
            {STAGE[stage]}评审{stage === "exit" ? ` · 第 ${node.round} 轮` : ""}
          </b>{" "}
          ·{" "}
          {stage === "exit"
            ? `规则：${RULE[spec.exit.rule]}。看的是执行人提交时选的文件（流程空间里的当前内容）；评审期间本节点会话不再写入`
            : `你是「${roleNames(s, spec.admit?.roles)}」。审核上游「${ups.map((u) => u.name).join("、")}」提交的产出物：能开工就准入，不能就打回上游`}
        </div>
      </div>
      <div className="row" style={{ flexWrap: "wrap" }}>
        {files.map((f) => (
          <FileChip key={f} path={f} onClick={() => setPreview(f)} />
        ))}
      </div>
      {preview && <FilePreview processId={process.id} path={preview} label="当前内容" />}
      {review && <Votes review={review} nameOf={nameOf} />}
      <div className="pcard">
        <h3>
          <Icon name="check" size={14} />
          你的结论
        </h3>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          {opts.map(([v, t, sub, color]) => (
            <div key={v} className={`radio-card ${decision === v ? "on" : ""}`} onClick={() => setDecision(v)} style={{ padding: "9px 10px" }}>
              <input type="radio" readOnly checked={decision === v} style={{ marginTop: 3 }} />
              <div>
                <b className="sm" style={{ color }}>
                  {t}
                </b>
                <div className="xs muted">{sub}</div>
              </div>
            </div>
          ))}
        </div>
        {stage === "admit" && decision === "reject" && ups.length > 1 && (
          <Select
            style={{ width: "100%", marginTop: 8 }}
            value={returnTo}
            onChange={setReturnTo}
            options={ups.map((u) => ({ value: u.id, label: `打回「${u.name}」` }))}
          />
        )}
        <Input.TextArea
          style={{ marginTop: 8 }}
          rows={3}
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          maxLength={2000}
          placeholder={
            stage === "exit" ? "驳回时必填：要改什么（会作为下一轮的指令发给数字员工）" : "打回时必填：要上游改什么（会作为上游下一轮的指令）"
          }
        />
        <div className="row" style={{ marginTop: 10 }}>
          <span className="xs faint">评审可以跨天，不会超时</span>
          <span className="sp" />
          <Button type="primary" loading={busy} disabled={!decision || needWhy} onClick={() => void go()}>
            提交结论
          </Button>
        </div>
      </div>
    </div>
  );
}

function Votes({ review, nameOf }: { review: Review; nameOf: (id: string) => string }) {
  return (
    <div className="pcard">
      <h3>
        <Icon name="users" size={14} />
        评审人（进入评审时锁定 · {RULE[review.rule]}）
      </h3>
      {review.reviewers.map((u) => {
        const v = review.votes.find((x) => x.reviewer === u);
        return (
          <div key={u} className="vote">
            <Who id={u} name={nameOf(u)} size={22} />
            <div className="grow">
              {v ? (
                <>
                  <span className="rtag" style={v.decision === "approve" ? { background: "var(--success-soft)", color: "var(--success)" } : { background: "var(--danger-soft)", color: "var(--danger)" }}>
                    {v.decision === "approve" ? "同意" : "不同意"}
                  </span>
                  {v.comment && <div className="sm" style={{ marginTop: 3 }}>{v.comment}</div>}
                </>
              ) : (
                <span className="rtag" style={{ background: "var(--warning-soft)", color: "var(--warning)" }}>
                  等待
                </span>
              )}
            </div>
            {v && <span className="xs faint">{dt(v.at)}</span>}
          </div>
        );
      })}
    </div>
  );
}

function Tabbed({ process, node, detail, nameOf, agentName }: Props) {
  const s = process.structure;
  const spec = s.nodes[node.id]!;
  const ups = (upstreams(s)[node.id] ?? []).map((u) => process.nodes.find((n) => n.id === u)!).filter(Boolean);
  const sub = node.latest_submission;
  const [preview, setPreview] = useState<string | undefined>(sub?.paths[0]);
  const ownDir = useSpace(process.id, node.name);
  const touched = (ownDir.data?.entries ?? []).filter((e) => !e.is_dir).map((e) => e.path);
  // 开工时锁定的数字员工（Agent 平台的智能体 id）
  const atlasAgent = node.atlas_agent_id;

  const out = (
    <div className="col" style={{ gap: 12 }}>
      {sub && !["working", "pending"].includes(node.status) ? (
        <>
          <div className="row" style={{ flexWrap: "wrap" }}>
            {sub.paths.map((f) => (
              <FileChip key={f} path={f} onClick={() => setPreview(f)} />
            ))}
            <span className="xs faint">第 {sub.round} 轮提交</span>
          </div>
          {preview && <FilePreview processId={process.id} path={preview} label="流程空间当前内容" />}
        </>
      ) : node.status === "working" ? (
        <>
          <div className="banner info">
            <Icon name="send" size={15} />
            <div>本轮还没提交。点「提交评审」时从流程空间选这一轮的产出物{sub ? `（第 ${sub.round} 轮提交过：${sub.paths.join("、")}）` : ""}</div>
          </div>
          {touched.length ? (
            <>
              <div className="xs muted">「{node.name}/」下的文件</div>
              <div className="row" style={{ flexWrap: "wrap" }}>
                {touched.map((f) => (
                  <FileChip key={f} path={f} onClick={() => setPreview(f)} />
                ))}
              </div>
              {(preview ?? touched[0]) && <FilePreview processId={process.id} path={(preview ?? touched[0])!} label="当前内容" />}
            </>
          ) : (
            <div className="pcard sm muted">数字员工还没在「{node.name}/」下写文件</div>
          )}
        </>
      ) : (
        <div className="pcard sm muted">还没有产出物：开工后数字员工写进流程空间，提交评审时由执行人选</div>
      )}
    </div>
  );

  const input = ups.length ? (
    <div className="col" style={{ gap: 10 }}>
      {ups.map((u) => (
        <div key={u.id} className="pcard">
          <div className="row">
            <b className="sm">{u.name}</b>
            <span className="xs faint">{u.latest_submission ? `第 ${u.latest_submission.round} 轮提交` : "还没提交"}</span>
            <span className="sp" />
            <NodeStatusTag status={u.status} />
          </div>
          <div style={{ marginTop: 4 }}>
            {u.latest_submission?.paths.map((f) => <FileChip key={f} path={f} />) ?? <span className="sm muted">—</span>}
          </div>
        </div>
      ))}
      <div className="xs faint">上游提交的文件就在流程空间里，开工指令列出路径，数字员工直接读；节点之间不传对话历史</div>
    </div>
  ) : (
    <div className="pcard">
      <b className="sm">流程标题</b>
      <div className="sm" style={{ marginTop: 4 }}>
        {process.title}
      </div>
      <div className="xs muted" style={{ marginTop: 4 }}>
        首节点没有上游：执行人把资料传进流程空间，数字员工先找执行人问清楚
      </div>
    </div>
  );

  const cfg = (
    <div className="pcard">
      <dl className="kv" style={{ gridTemplateColumns: "82px 1fr" }}>
        <dt>工作说明</dt>
        <dd className="sm" style={{ whiteSpace: "pre-wrap" }}>
          {spec.instructions || "—"}
        </dd>
        <dt>执行</dt>
        <dd className="sm">
          {s.roles[spec.exec_role]}：{node.executors.map((u) => nameOf(u)).join("、") || "—"}
          {agentName(atlasAgent) && (
            <div style={{ marginTop: 4 }}>
              <AgentChip name={agentName(atlasAgent)!} size={16} />
            </div>
          )}
        </dd>
        <Skills team={process.team_id} atlasId={atlasAgent} />
        <dt>产出目录</dt>
        <dd className="sm">
          <span className="mono">{node.name}/</span>
          <div className="xs faint">开工指令里的约定；提交时由执行人选文件，不限目录</div>
        </dd>
        {spec.admit && (
          <>
            <dt>准入</dt>
            <dd className="sm">
              {roleNames(s, spec.admit.roles)} · {RULE[spec.admit.rule]}
            </dd>
          </>
        )}
        <dt>准出</dt>
        <dd className="sm">
          {roleNames(s, spec.exit.roles)} · {RULE[spec.exit.rule]}
        </dd>
      </dl>
    </div>
  );

  const history = detail?.reviews.length ? (
    <div className="pcard">
      {detail.reviews.map((r) => (
        <div key={r.id} className="vote">
          <span className="rtag" style={{ marginTop: 3 }}>
            {STAGE[r.stage]} · 第 {r.round} 轮
          </span>
          <div className="grow">
            <div className="row" style={{ flexWrap: "wrap" }}>
              {r.submissions.length ? r.submissions.map((x) => <FileChip key={x} path={x} />) : null}
            </div>
            {r.votes.map((v, i) => (
              <div key={i} style={{ marginTop: 6 }}>
                <div className="row">
                  <Who id={v.reviewer} name={nameOf(v.reviewer)} size={18} />
                  <span className="rtag" style={v.decision === "approve" ? { background: "var(--success-soft)", color: "var(--success)" } : { background: "var(--danger-soft)", color: "var(--danger)" }}>
                    {v.decision === "approve" ? "同意" : v.return_to ? "打回上游" : "驳回修改"}
                  </span>
                  <span className="xs faint">{dt(v.at)}</span>
                </div>
                {v.comment && <div className="sm" style={{ marginTop: 3 }}>{v.comment}</div>}
              </div>
            ))}
            {r.status === "open" && <div className="xs warn-text" style={{ marginTop: 4 }}>评审中</div>}
          </div>
        </div>
      ))}
    </div>
  ) : (
    <div className="pcard sm muted">还没有评审</div>
  );

  return (
    <div className="rp">
      {node.open_review && (
        <div className="banner warn">
          <Icon name="shield" size={15} />
          <div>
            {STAGE[node.open_review.stage]}评审中：等 {node.open_review.reviewers.map((u) => nameOf(u)).join("、")}
          </div>
        </div>
      )}
      <Tabs
        size="small"
        items={[
          { key: "out", label: "产出物", children: out },
          { key: "in", label: "输入", children: input },
          {
            key: "space",
            label: "流程空间",
            children: (
              <div className="pcard sm">
                本节点的会话把流程空间挂在 <span className="mono">/space</span>，上游、并行节点的文件都在里面 ·{" "}
                <Link href={`/flows/${process.id}/space`}>打开流程空间</Link>
              </div>
            ),
          },
          { key: "cfg", label: "本节点", children: cfg },
          { key: "rev", label: "评审记录", children: history },
        ]}
      />
    </div>
  );
}

function Skills({ team, atlasId }: { team: string; atlasId: string | null | undefined }) {
  // 卡片接口按团队内的 id 查；节点上记的是 Agent 平台的 id，先在团队的数字员工里找到它
  const card = useAgentCardByAtlasId(team, atlasId);
  if (!card?.skills.length) return null;
  return (
    <>
      <dt>技能</dt>
      <dd className="sm">
        {card.skills.map((k) => (
          <span key={k} className="rtag" style={{ marginRight: 4 }}>
            {k}
          </span>
        ))}
        <div className="xs faint">产出物的格式由数字员工的技能约定</div>
      </dd>
    </>
  );
}


function useAgentCardByAtlasId(team: string, atlasId: string | null | undefined) {
  const agents = useTeamAgents(team);
  const teamAgentId = agents.data?.find((a) => a.atlas_agent_id === atlasId)?.id;
  return useAgentCard(team, teamAgentId).data?.card ?? null;
}
