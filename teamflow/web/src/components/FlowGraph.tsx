"use client";

/**
 * 流程图：步骤从左到右，并行组内的轨道上下排列；节点卡上是名字、轮次、状态、执行、准入 / 准出由谁审。
 *
 * ★ 只按结构画（依赖由结构推出来，不画连线）。流程详情传节点状态，编排器传选中的节点。
 */
import Link from "next/link";
import type { ReactNode } from "react";

import { NODE_STATUS, RULE } from "@/lib/format";
import { roleNames } from "@/lib/structure";
import type { NodeStatus, Structure } from "@/lib/types";

import { Avatar, Icon, NodeStatusTag } from "./ui";

export interface NodeState {
  status: NodeStatus;
  round: number;
  agent?: string | null;
}

interface Props {
  structure: Structure;
  /** 流程里每个节点的状态（流程详情）；不给 = 只画模板 */
  states?: Record<string, NodeState>;
  /** 点节点去哪（流程详情 → 节点页） */
  href?: (id: string) => string;
  /** 选中的节点、点节点选中（编排器、市场详情） */
  selected?: string;
  onSelect?: (id: string) => void;
  /** 有问题的节点（编排器校验） */
  bad?: Set<string>;
  /** 编排器：每个步骤后面、并行组里的加号 */
  renderAfterStep?: (stepIndex: number) => ReactNode;
  renderInPar?: (stepIndex: number) => ReactNode;
  renderAfterTrack?: (stepIndex: number, track: number) => ReactNode;
  renderUnderStep?: (stepIndex: number) => ReactNode;
  head?: ReactNode;
}

export function FlowGraph(p: Props) {
  const s = p.structure;
  const box = (id: string) => {
    const n = s.nodes[id];
    if (!n) return null;
    const st = p.states?.[id];
    const color = st ? NODE_STATUS[st.status] : null;
    const body = (
      <>
        <div className="nm">
          <span className="grow ellipsis">{n.name || "（未命名）"}</span>
          {st && st.round > 1 && <span className="rtag">第 {st.round} 轮</span>}
        </div>
        {st && (
          <div style={{ marginTop: 6 }}>
            <NodeStatusTag status={st.status} />
          </div>
        )}
        <div className="whoexec">
          {st?.agent ? (
            <>
              <Avatar name={st.agent} size={16} bot />
              <span className="ellipsis">{st.agent}</span>
            </>
          ) : (
            <>
              <Icon name="bot" size={13} />
              <span className="ellipsis">执行：{s.roles[n.exec_role] ?? (n.exec_role || "未选")}</span>
            </>
          )}
        </div>
        <div className="rv">
          {n.admit && (
            <span className="rtag admit" title="准入评审：开工前审核上游提交的产出物">
              准入 {roleNames(s, n.admit.roles) || "—"}
            </span>
          )}
          <span className="rtag exit" title={`准出评审，${RULE[n.exit?.rule ?? "any"]}`}>
            准出 {roleNames(s, n.exit?.roles) || "—"}
          </span>
        </div>
      </>
    );
    const cls = `fnode ${p.selected === id ? "on" : ""} ${p.bad?.has(id) ? "bad" : ""}`;
    const style = color ? { borderColor: color.color + "66" } : undefined;
    if (p.href)
      return (
        <Link key={id} href={p.href(id)} className={cls} style={style}>
          {body}
        </Link>
      );
    return (
      <div key={id} className={cls} style={style} onClick={() => p.onSelect?.(id)}>
        {body}
      </div>
    );
  };

  return (
    <div className="flow">
      {p.head}
      {s.flow.map((step, i) => (
        <span key={i} style={{ display: "contents" }}>
          {i > 0 && <span className="arrow" />}
          <span style={{ display: "flex", flexDirection: "column", alignItems: "center", flex: "none" }}>
            {step.type === "node" ? (
              box(step.id)
            ) : (
              <div className="par">
                <span className="plabel">并行</span>
                {step.tracks.map((t, ti) => (
                  <div key={ti} className="track">
                    {t.nodes.map((id, j) => (
                      <span key={id} style={{ display: "contents" }}>
                        {j > 0 && <span className="arrow" />}
                        {box(id)}
                      </span>
                    ))}
                    {p.renderAfterTrack?.(i, ti)}
                  </div>
                ))}
                {p.renderInPar?.(i)}
              </div>
            )}
            {p.renderUnderStep?.(i)}
          </span>
          {p.renderAfterStep?.(i)}
        </span>
      ))}
    </div>
  );
}

export function Legend({ extra }: { extra?: ReactNode }) {
  return (
    <div className="legend">
      {(Object.keys(NODE_STATUS) as NodeStatus[]).map((k) => (
        <NodeStatusTag key={k} status={k} />
      ))}
      <span className="sp" />
      {extra}
      <span className="rtag admit">准入</span>
      <span>开工前审上游</span>
      <span className="rtag exit">准出</span>
      <span>交付前审本节点</span>
    </div>
  );
}
