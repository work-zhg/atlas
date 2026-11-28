"use client";

/**
 * 流程 · 记录：从发起到结束的里程碑，按步骤分组（谁开始、谁下指令、谁上传、谁提交、谁同意或退回）。
 * 系统内部的步骤不显示；对话原文点「打开」到步骤里看。导出的是全部原始事件。
 */
import { Button, Checkbox, Select } from "antd";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { FlowHeader } from "@/components/FlowHeader";
import { Icon, LoadError, Loading, NodeStatusTag, Page, StepNo } from "@/components/ui";
import { describe } from "@/lib/events";
import { dt } from "@/lib/format";
import { useFlow } from "@/lib/flow";
import { nodeIds, stepNos } from "@/lib/structure";

export default function RecordsPage() {
  const { id } = useParams<{ id: string }>();
  const { proc, events, nameOf, nodeName, version } = useFlow(id);
  const [node, setNode] = useState("all");
  const [reviewOnly, setReviewOnly] = useState(false);

  if (proc.error) return <LoadError error={proc.error} />;
  if (!proc.data || !events.data) return <Loading />;
  const p = proc.data;
  const no = stepNos(p.structure);
  const rows = events.data
    .map((e) => ({ e, m: describe(e, nameOf, nodeName) }))
    .filter((x): x is { e: (typeof x)["e"]; m: NonNullable<(typeof x)["m"]> } => !!x.m)
    .filter(({ e, m }) => (node === "all" || (e.node_id ?? "") === node) && (!reviewOnly || m.review));
  const groups: [string, string][] = [["", "流程"], ...nodeIds(p.structure).map((n): [string, string] => [n, nodeName(n)])];
  const byId = Object.fromEntries(p.nodes.map((n) => [n.id, n]));

  const exportJson = () => {
    const blob = new Blob([JSON.stringify({ process: { code: p.code, title: p.title }, events: events.data }, null, 2)], {
      type: "application/json",
    });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${p.code}-记录.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <Page>
      <FlowHeader process={p} tab="records" version={version} nameOf={nameOf} />
      <div className="row" style={{ marginBottom: 12 }}>
        <Select
          style={{ width: 160 }}
          value={node}
          onChange={setNode}
          options={[{ value: "all", label: "全部步骤" }, ...nodeIds(p.structure).map((n) => ({ value: n, label: `${no[n]} ${nodeName(n)}` }))]}
        />
        <Checkbox checked={reviewOnly} onChange={(e) => setReviewOnly(e.target.checked)}>
          只看评审
        </Checkbox>
        <span className="sp" />
        <Button icon={<Icon name="download" size={14} />} onClick={exportJson}>
          导出
        </Button>
      </div>
      <div className="card pad">
        <div className="tline">
          {groups
            .filter(([n]) => rows.some(({ e }) => (e.node_id ?? "") === n))
            .map(([n, name]) => (
              <div key={n || "_"}>
                <div className="tnode">
                  {n && <StepNo n={no[n]} status={byId[n]?.status} />}
                  {name}
                  {n && byId[n] && <NodeStatusTag status={byId[n]!.status} />}
                  {n && (
                    <Link className="sm" style={{ fontWeight: 500 }} href={`/flows/${p.id}/n/${n}`}>
                      打开
                    </Link>
                  )}
                </div>
                {rows
                  .filter(({ e }) => (e.node_id ?? "") === n)
                  .map(({ e, m }) => (
                    <div key={e.seq} className="tev">
                      <span className={`dot2 k-${m.kind}`} />
                      <div className="row" style={{ alignItems: "flex-start" }}>
                        <span className="xs faint" style={{ width: 84, flex: "none", marginTop: 2 }}>
                          {dt(e.at)}
                        </span>
                        <span className="sm">{m.text}</span>
                      </div>
                    </div>
                  ))}
              </div>
            ))}
          {!rows.length && <div className="sm muted">没有这类记录</div>}
        </div>
      </div>
    </Page>
  );
}
