"use client";

import { Tag } from "antd";

import { RULE } from "@/lib/format";
import type { Definition, Issue, ProcessNodeState } from "@/lib/types";

const STATUS_STYLE: Record<string, { border: string; bg: string; label: string; color: string }> = {
  pending: { border: "#e6e8ee", bg: "#fff", label: "待开始", color: "default" },
  working: { border: "#0ea5e9", bg: "#f0f9ff", label: "人机协同中", color: "processing" },
  exit_review: { border: "#f59e0b", bg: "#fffbeb", label: "待准出", color: "warning" },
  admit_review: { border: "#f59e0b", bg: "#fffbeb", label: "待准入", color: "warning" },
  passed: { border: "#16a34a", bg: "#f0fdf4", label: "已通过", color: "success" },
  returned: { border: "#dc2626", bg: "#fef2f2", label: "已打回", color: "error" },
};

/** 只读的流程结构图：步骤从左到右，并行组内轨道上下排列。 */
export function FlowView({
  definition,
  active,
  onPick,
  issues = [],
  states,
}: {
  definition: Definition;
  active?: string;
  onPick?: (nodeId: string) => void;
  issues?: Issue[];
  /** 流程运行时的节点状态：传了就按状态着色 */
  states?: Record<string, ProcessNodeState>;
}) {
  const bad = new Set(issues.map((i) => i.node).filter(Boolean));
  const node = (id: string) => {
    const n = definition.nodes[id];
    const st = states?.[id];
    const style = st ? STATUS_STYLE[st.status] : undefined;
    return (
      <div
        key={id}
        className={`tf-node${active === id ? " active" : ""}${bad.has(id) ? " bad" : ""}`}
        onClick={() => onPick?.(id)}
        style={style && active !== id ? { borderColor: style.border, background: style.bg } : style ? { background: style.bg } : undefined}
      >
        <div className="t">{n?.name || "未命名节点"}</div>
        {st && (
          <div style={{ margin: "2px 0 6px" }}>
            <Tag color={style!.color} style={{ marginInlineEnd: 4 }}>
              {style!.label}
            </Tag>
            {st.round > 1 && <Tag>第 {st.round} 轮</Tag>}
            {st.agent_running && <Tag color="cyan">Agent 工作中</Tag>}
            {st.notice && <Tag color="red">需关注</Tag>}
          </div>
        )}
        <div className="m">
          <div>⚙️ {n?.exec_role || <span style={{ color: "#dc2626" }}>缺执行角色</span>}</div>
          <div>📄 {n?.output_name || "—"}</div>
          <div>
            ✅ {n?.exit_review.role || "—"}
            {n?.exit_review.role && <span> · {RULE[n.exit_review.rule]}</span>}
          </div>
          {n?.admit_review && (
            <div>
              🚪 {n.admit_review.role} · {RULE[n.admit_review.rule]}
            </div>
          )}
        </div>
      </div>
    );
  };
  return (
    <div className="tf-flow">
      {definition.flow.map((step, i) => (
        <div key={i} style={{ display: "flex", gap: 10, alignItems: "center" }}>
          {i > 0 && <span className="tf-arrow">→</span>}
          {step.type === "node" ? (
            node(step.id)
          ) : (
            <div className="tf-par">
              <span className="tf-par-label">并行 · {step.tracks.length} 条轨道</span>
              {step.tracks.map((t, ti) => (
                <div key={ti} className="tf-track">
                  {t.nodes.map((id, ni) => (
                    <div key={id} style={{ display: "flex", gap: 8, alignItems: "center" }}>
                      {ni > 0 && <span className="tf-arrow">→</span>}
                      {node(id)}
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

export function RoleTags({ roles }: { roles: { exec: string[]; review: string[] } }) {
  return (
    <>
      {roles.exec.map((r) => (
        <Tag key={`e-${r}`} color="blue">
          ⚙️ {r}
        </Tag>
      ))}
      {roles.review.map((r) => (
        <Tag key={`r-${r}`} color="purple">
          ✅ {r}
        </Tag>
      ))}
    </>
  );
}
