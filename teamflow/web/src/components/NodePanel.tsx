"use client";

/**
 * 步骤页右侧：轮到我评审时是评审区；否则是 交付 · 参考资料 · 说明 · 评审记录。
 *
 * ★ 交付：负责人在这里勾文件、提交评审（默认勾上这一步文件夹里的文件；别的文件夹的另外选）。
 *   只记这一轮交了哪些文件，文件本身不做版本管理。
 * ★ 准出评审：审这一步交的文件 → 同意 / 退回修改（这一步再做一轮）。
 *   准入评审：审上一步交的文件 → 同意，开始这一步 / 退回上一步（上一步重做）。退回必须写意见，意见就是下一轮的要求。
 * ★ 文件点了都在新标签页打开（这里太窄，看不全）。
 */
import { useQueryClient } from "@tanstack/react-query";
import { App, Button, Checkbox, Input, Select, Tabs } from "antd";
import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";

import { api, errorText } from "@/lib/api";
import { ago, base, dt, MATERIALS_DIR, NODE_STATUS, RULE, STAGE } from "@/lib/format";
import { useAgentCard, useSpace, useTeamAgents } from "@/lib/queries";
import { roleNames, upstreams } from "@/lib/structure";
import type { NodeDetail, ProcessDetail, ProcessNode, Review, ReviewSpec } from "@/lib/types";

import { PickFilesModal } from "./PickFilesModal";
import { AgentChip, FileChip, Icon, viewHref, Who } from "./ui";

interface Props {
  process: ProcessDetail;
  node: ProcessNode;
  detail: NodeDetail | undefined;
  nameOf: (id: string | null | undefined) => string;
  agent: string | null;
}

export function NodePanel(p: Props) {
  const open = p.node.open_review;
  if (open?.i_can_vote) return <Decide {...p} stage={open.stage} />;
  return <Tabbed {...p} />;
}

/** 评审人：角色名 + 现在是谁 */
function reviewers(p: ProcessDetail, spec: ReviewSpec, nameOf: Props["nameOf"]) {
  const people = [...new Set(spec.roles.flatMap((r) => p.roles[r] ?? []))].map(nameOf);
  return `${people.join("、") || roleNames(p.structure, spec.roles)}（${roleNames(p.structure, spec.roles)}，${RULE[spec.rule]}）`;
}

/** 一行文件：点名字在新标签页打开。label 不给就显示完整路径 */
function FileRow({ processId, path, label, extra, check }: { processId: string; path: string; label?: string; extra?: ReactNode; check?: ReactNode }) {
  return (
    <div className="pickrow" style={{ cursor: "default" }}>
      {check}
      <Icon name="file" size={14} />
      <Link className="grow ellipsis" href={viewHref(processId, path)} target="_blank" rel="noopener" title={`${path}（在新标签页打开）`} style={{ color: "var(--fg)" }}>
        {label ?? path}
      </Link>
      {extra}
    </div>
  );
}

// ───────────────────────────── 评审区

function Decide({ process, node, detail, nameOf, agent, stage }: Props & { stage: "admit" | "exit" }) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const s = process.structure;
  const review = detail?.reviews.find((r) => r.status === "open" && r.stage === stage);
  const ups = (upstreams(s)[node.id] ?? []).map((u) => process.nodes.find((n) => n.id === u)!).filter(Boolean);
  const subs = stage === "exit" ? [node.latest_submission] : ups.map((u) => u.latest_submission);
  const files = review?.files ?? subs.flatMap((x) => x?.paths ?? []);
  const by = subs.find((x) => x)?.submitted_by;
  const at = subs.find((x) => x)?.submitted_at;
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
          ["approve", "同意", "交给下一步", "var(--success)"],
          ["reject", "退回修改", "这一步再做一轮", "var(--danger)"],
        ] as const)
      : ([
          ["approve", "同意，开始这一步", "数字员工接着做", "var(--success)"],
          ["reject", "退回上一步", "上一步重做一轮", "var(--danger)"],
        ] as const);
  const needWhy = decision === "reject" && !comment.trim();

  return (
    <div className="rp">
      <div className={`banner ${stage === "exit" ? "warn" : "purple"}`}>
        <Icon name="shield" size={15} />
        <div>
          <b>
            等你{STAGE[stage]}评审{review ? ` · 第 ${review.round} 轮` : ""}
          </b>
          <div className="sm">
            {stage === "exit"
              ? `${by ? nameOf(by) : "负责人"} 提交了 ${files.length} 个文件${at ? ` · ${ago(at)}` : ""}`
              : `上一步「${ups.map((u) => u.name).join("、")}」交付了 ${files.length} 个文件：能开始就同意，不能就退回上一步`}
          </div>
        </div>
      </div>
      <div className="col" style={{ gap: 6 }}>
        {files.map((f) => (
          <FileRow key={f} processId={process.id} path={f} extra={<Icon name="right" size={13} />} />
        ))}
        <div className="xs faint">点文件在新标签页打开</div>
      </div>
      <div className="pcard">
        <h3>你的结论</h3>
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
            options={ups.map((u) => ({ value: u.id, label: `退回「${u.name}」` }))}
          />
        )}
        <Input.TextArea
          style={{ marginTop: 8 }}
          rows={3}
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          maxLength={2000}
          placeholder={
            stage === "exit"
              ? `意见（退回时必填：会发给${agent ?? "数字员工"}，作为下一轮的要求）`
              : "意见（退回时必填：会发给上一步，作为重做的要求）"
          }
        />
        <div className="row" style={{ marginTop: 10 }}>
          <span className="sp" />
          <Button type="primary" loading={busy} disabled={!decision || needWhy} onClick={() => void go()}>
            提交结论
          </Button>
        </div>
      </div>
      {review && review.reviewers.length > 1 && <Votes review={review} nameOf={nameOf} title={`评审人（${RULE[review.rule]}）`} />}
    </div>
  );
}

function Votes({ review, nameOf, title }: { review: Review; nameOf: Props["nameOf"]; title: string }) {
  return (
    <div className="pcard">
      <h3>{title}</h3>
      {review.reviewers.map((u) => {
        const v = review.votes.find((x) => x.reviewer === u);
        return (
          <div key={u} className="vote">
            <Who id={u} name={nameOf(u)} size={20} />
            <div className="grow">
              {v ? <Decision v={v} /> : <span className="rtag" style={{ background: "var(--warning-soft)", color: "var(--warning)" }}>还没评</span>}
              {v?.comment && <div className="sm" style={{ marginTop: 3 }}>{v.comment}</div>}
            </div>
            {v && <span className="xs faint">{dt(v.at)}</span>}
          </div>
        );
      })}
    </div>
  );
}

function Decision({ v }: { v: Review["votes"][number] }) {
  const ok = v.decision === "approve";
  return (
    <span className="rtag" style={ok ? { background: "var(--success-soft)", color: "var(--success)" } : { background: "var(--danger-soft)", color: "var(--danger)" }}>
      {ok ? "同意" : v.return_to ? "退回上一步" : "退回修改"}
    </span>
  );
}

// ───────────────────────────── 四个页签

function Tabbed(p: Props) {
  const { process, node, nameOf } = p;
  const s = process.structure;
  const spec = s.nodes[node.id]!;
  const mineToSubmit = node.i_am_executor && process.status === "running" && node.status === "working";
  const [tab, setTab] = useState(node.status === "pending" ? "in" : "out");

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
        activeKey={tab}
        onChange={setTab}
        items={[
          { key: "out", label: "交付", children: mineToSubmit ? <Deliver {...p} /> : <Delivered {...p} /> },
          { key: "in", label: "参考资料", children: <Inputs {...p} /> },
          { key: "cfg", label: "说明", children: <About {...p} spec={spec} /> },
          { key: "rev", label: `评审记录${p.detail?.reviews.length ? `（${p.detail.reviews.length}）` : ""}`, children: <History {...p} /> },
        ]}
      />
    </div>
  );
}

/** 负责人提交：勾这一步文件夹里的文件（可以再从别的文件夹加），提交评审 */
function Deliver({ process, node, nameOf }: Props) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const spec = process.structure.nodes[node.id]!;
  const own = useSpace(process.id, node.name);
  const files = (own.data?.entries ?? []).filter((e) => !e.is_dir);
  const [sel, setSel] = useState<string[] | null>(null);
  const [extra, setExtra] = useState<string[]>([]);
  const [picking, setPicking] = useState(false);
  const [busy, setBusy] = useState(false);
  // 默认：上一轮交过的接着交；没交过就勾上文件夹里的全部
  useEffect(() => {
    if (sel !== null || !own.data) return;
    const last = node.latest_submission?.paths ?? [];
    setSel(last.length ? last : files.map((f) => f.path));
    setExtra(last.filter((x) => !files.some((f) => f.path === x)));
  }, [own.data, sel, files, node.latest_submission]);
  const chosen = sel ?? [];
  const toggle = (x: string) => setSel(chosen.includes(x) ? chosen.filter((y) => y !== x) : [...chosen, x]);

  const submit = async () => {
    setBusy(true);
    try {
      await api.post(`/processes/${process.id}/nodes/${node.id}/submissions`, { paths: chosen });
      await qc.invalidateQueries({ queryKey: ["process", process.id] });
      await qc.invalidateQueries({ queryKey: ["node", process.id, node.id] });
      message.success("已提交评审");
    } catch (e) {
      message.error(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const rows = [...files.map((f) => ({ path: f.path, at: f.modified_at })), ...extra.map((x) => ({ path: x, at: null }))];
  return (
    <div className="col" style={{ gap: 8 }}>
      <div className="sm">
        {rows.length ? "勾上这一轮要交的文件：" : `「${node.name}」文件夹里还没有文件：数字员工写好后会出现在这里`}
      </div>
      {rows.map((r) => (
        <FileRow
          key={r.path}
          processId={process.id}
          path={r.path}
          label={r.path.startsWith(`${node.name}/`) ? base(r.path) : r.path}
          check={<Checkbox checked={chosen.includes(r.path)} onChange={() => toggle(r.path)} />}
          extra={r.at && <span className="xs faint">{ago(r.at)}</span>}
        />
      ))}
      <div className="row">
        <a className="xs" onClick={() => setPicking(true)}>
          从其他文件夹选
        </a>
        <span className="sp" />
        <Button type="primary" icon={<Icon name="send" size={14} />} loading={busy} disabled={!chosen.length} onClick={() => void submit()}>
          提交评审{chosen.length ? `（${chosen.length} 个文件）` : ""}
        </Button>
      </div>
      <div className="xs faint">
        评审人：{reviewers(process, spec.exit, nameOf)}。评审期间数字员工暂停
      </div>
      {picking && (
        <PickFilesModal
          processId={process.id}
          startDir=""
          picked={chosen}
          onClose={() => setPicking(false)}
          onPick={(paths) => {
            setExtra((x) => [...new Set([...x, ...paths.filter((y) => !files.some((f) => f.path === y))])]);
            setSel([...new Set([...chosen, ...paths])]);
            setPicking(false);
          }}
        />
      )}
    </div>
  );
}

/** 别人看、或者不在进行中：最近一轮交了什么，加上这一步文件夹里的文件 */
function Delivered({ process, node }: Props) {
  const own = useSpace(process.id, node.name);
  const files = (own.data?.entries ?? []).filter((e) => !e.is_dir);
  const sub = node.latest_submission;
  return (
    <div className="col" style={{ gap: 12 }}>
      {sub ? (
        <div className="pcard">
          <div className="row">
            <b className="sm">第 {sub.round} 轮交付</b>
            <span className="xs faint">{ago(sub.submitted_at)}</span>
            <span className="sp" />
            <span className="xs muted">{NODE_STATUS[node.status].label}</span>
          </div>
          <div style={{ marginTop: 4 }}>
            {sub.paths.map((f) => (
              <FileChip key={f} processId={process.id} path={f} />
            ))}
          </div>
        </div>
      ) : (
        <div className="sm muted">还没交付过</div>
      )}
      <div>
        <div className="xs muted" style={{ marginBottom: 4 }}>
          「{node.name}」文件夹
        </div>
        {files.length ? (
          files.map((f) => <FileRow key={f.path} processId={process.id} path={f.path} label={f.name} extra={f.modified_at && <span className="xs faint">{ago(f.modified_at)}</span>} />)
        ) : (
          <div className="sm muted">{node.status === "pending" ? "还没开始" : "数字员工还没写文件"}</div>
        )}
      </div>
    </div>
  );
}

/** 参考资料：上一步交付的文件 + 成员上传的资料 */
function Inputs({ process, node }: Props) {
  const ups = (upstreams(process.structure)[node.id] ?? []).map((u) => process.nodes.find((n) => n.id === u)!).filter(Boolean);
  const materials = useSpace(process.id, MATERIALS_DIR);
  const docs = (materials.data?.entries ?? []).filter((e) => !e.is_dir);
  return (
    <div className="col" style={{ gap: 10 }}>
      {!ups.length && (
        <div className="pcard">
          <div className="xs muted">这条流程要做的事</div>
          <div className="sm" style={{ marginTop: 2 }}>
            {process.title}
          </div>
        </div>
      )}
      {ups.map((u) => (
        <div key={u.id} className="pcard">
          <div className="row">
            <b className="sm">上一步「{u.name}」交付的</b>
            <span className="sp" />
            <span className="xs faint">{u.latest_submission ? `第 ${u.latest_submission.round} 轮` : "还没交付"}</span>
          </div>
          <div style={{ marginTop: 4 }}>
            {u.latest_submission?.paths.map((f) => <FileChip key={f} processId={process.id} path={f} />) ?? <span className="sm muted">—</span>}
          </div>
        </div>
      ))}
      <div className="pcard">
        <div className="row">
          <b className="sm">资料</b>
          <span className="xs faint">成员上传的</span>
          <span className="sp" />
          <Link className="xs" href={`/flows/${process.id}/space?dir=${encodeURIComponent(MATERIALS_DIR)}`}>
            去上传
          </Link>
        </div>
        <div style={{ marginTop: 4 }}>
          {docs.length ? docs.map((f) => <FileChip key={f.path} processId={process.id} path={f.path} />) : <span className="sm muted">还没有</span>}
        </div>
      </div>
      <div className="xs faint">数字员工开始时会收到这些文件的清单，直接读</div>
    </div>
  );
}

function About({ process, node, nameOf, agent, spec }: Props & { spec: ProcessDetail["structure"]["nodes"][string] }) {
  const s = process.structure;
  return (
    <div className="pcard">
      <dl className="kv" style={{ gridTemplateColumns: "72px 1fr" }}>
        <dt>工作说明</dt>
        <dd className="sm" style={{ whiteSpace: "pre-wrap" }}>
          {spec.instructions || "—"}
        </dd>
        <dt>负责</dt>
        <dd className="sm">
          <div className="row" style={{ flexWrap: "wrap" }}>
            <span className="muted">{s.roles[spec.exec_role]}</span>
            {node.executors.map((u) => (
              <Who key={u} id={u} name={nameOf(u)} size={18} />
            ))}
            {!node.executors.length && <span className="faint">没配人</span>}
          </div>
          {agent && (
            <div style={{ marginTop: 6 }}>
              <AgentChip name={agent} size={18} />
              <Skills team={process.team_id} atlasId={node.atlas_agent_id} />
            </div>
          )}
        </dd>
        {spec.admit && (
          <>
            <dt>准入评审</dt>
            <dd className="sm">{reviewers(process, spec.admit, nameOf)}</dd>
          </>
        )}
        <dt>准出评审</dt>
        <dd className="sm">{reviewers(process, spec.exit, nameOf)}</dd>
      </dl>
    </div>
  );
}

function History({ process, detail, nameOf }: Props) {
  if (!detail?.reviews.length) return <div className="sm muted">还没有评审</div>;
  return (
    <div className="col" style={{ gap: 10 }}>
      {[...detail.reviews].reverse().map((r) => (
        <div key={r.id} className="pcard">
          <div className="row">
            <b className="sm">
              {STAGE[r.stage]}评审 · 第 {r.round} 轮
            </b>
            <span className="xs faint">{dt(r.opened_at)}</span>
            <span className="sp" />
            {r.status === "open" ? (
              <span className="rtag exit">评审中</span>
            ) : r.status === "approved" ? (
              <span className="rtag" style={{ background: "var(--success-soft)", color: "var(--success)" }}>通过</span>
            ) : r.status === "rejected" ? (
              <span className="rtag" style={{ background: "var(--danger-soft)", color: "var(--danger)" }}>退回</span>
            ) : (
              <span className="rtag">取消</span>
            )}
          </div>
          <div style={{ marginTop: 2 }}>
            {r.files.map((x) => (
              <FileChip key={x} processId={process.id} path={x} />
            ))}
          </div>
          {r.votes.map((v, i) => (
            <div key={i} style={{ marginTop: 8 }}>
              <div className="row">
                <Who id={v.reviewer} name={nameOf(v.reviewer)} size={18} />
                <Decision v={v} />
                <span className="xs faint">{dt(v.at)}</span>
              </div>
              {v.comment && <div className="sm" style={{ marginTop: 3, whiteSpace: "pre-wrap" }}>{v.comment}</div>}
            </div>
          ))}
          {r.status === "open" && (
            <div className="xs warn-text" style={{ marginTop: 6 }}>
              等 {r.reviewers.filter((u) => !r.votes.some((v) => v.reviewer === u)).map(nameOf).join("、")}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function Skills({ team, atlasId }: { team: string; atlasId: string | null | undefined }) {
  // 卡片接口按团队内的 id 查；步骤上记的是 Agent 平台的 id，先在团队的数字员工里找到它
  const agents = useTeamAgents(team);
  const teamAgentId = agents.data?.find((a) => a.atlas_agent_id === atlasId)?.id;
  const card = useAgentCard(team, teamAgentId).data?.card;
  if (!card?.skills.length) return null;
  return (
    <div className="row" style={{ flexWrap: "wrap", gap: 4, marginTop: 4 }}>
      <span className="xs muted">技能</span>
      {card.skills.map((k) => (
        <span key={k} className="rtag">
          {k}
        </span>
      ))}
    </div>
  );
}
