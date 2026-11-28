"use client";

/**
 * 追溯：一条流程从发起到结束的全部事件，按节点分组。每轮评审记的是「这一轮提交了哪些文件」和结论；
 * 文件本身不留历史版本，对话原文点「打开节点」看。
 */
import { Button, Select } from "antd";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { Crumb, Icon, LoadError, Loading, NodeStatusTag, Page, PageHead } from "@/components/ui";
import { describe, type EventKind } from "@/lib/events";
import { dt } from "@/lib/format";
import { useFlow } from "@/lib/flow";
import { useProject } from "@/lib/queries";
import { nodeIds } from "@/lib/structure";

const FILTERS: { key: string; label: string; kinds: EventKind[] }[] = [
  { key: "all", label: "全部事件", kinds: [] },
  { key: "review", label: "提交与评审", kinds: ["review", "pass", "reject"] },
  { key: "lock", label: "开工与锁定", kinds: ["lock"] },
  { key: "save", label: "上传", kinds: ["save"] },
];

export default function TracePage() {
  const { id } = useParams<{ id: string }>();
  const { proc, events, nameOf, nodeName } = useFlow(id);
  const project = useProject(proc.data?.project_id);
  const [node, setNode] = useState("all");
  const [kind, setKind] = useState("all");

  if (proc.error) return <LoadError error={proc.error} />;
  if (!proc.data || !events.data) return <Loading />;
  const p = proc.data;
  const kinds = FILTERS.find((f) => f.key === kind)!.kinds;
  const rows = events.data
    .map((e) => ({ e, d: describe(e, nameOf, nodeName) }))
    .filter(({ e, d }) => (node === "all" || (e.node_id ?? "") === node) && (!kinds.length || kinds.includes(d.kind)));
  const groups: [string, string][] = [["", "流程"], ...nodeIds(p.structure).map((n): [string, string] => [n, nodeName(n)])];
  const byId = Object.fromEntries(p.nodes.map((n) => [n.id, n]));

  const exportJson = () => {
    const blob = new Blob([JSON.stringify({ process: { code: p.code, title: p.title }, events: events.data }, null, 2)], {
      type: "application/json",
    });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${p.code}-追溯.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <Page narrow>
      <Crumb
        items={[
          [project.data?.name ?? "项目", `/projects/${p.project_id}`],
          [`${p.code} ${p.title}`, `/flows/${p.id}`],
          ["追溯", undefined],
        ]}
      />
      <PageHead
        title={`追溯 · ${p.title}`}
        extra={
          <>
            <Select
              style={{ width: 150 }}
              value={node}
              onChange={setNode}
              options={[{ value: "all", label: "全部节点" }, ...nodeIds(p.structure).map((n) => ({ value: n, label: nodeName(n) }))]}
            />
            <Select style={{ width: 130 }} value={kind} onChange={setKind} options={FILTERS.map((f) => ({ value: f.key, label: f.label }))} />
            <Button icon={<Icon name="download" size={14} />} onClick={exportJson}>
              导出
            </Button>
          </>
        }
      />
      <div className="card pad">
        <div className="tline">
          {groups
            .filter(([n]) => rows.some(({ e }) => (e.node_id ?? "") === n))
            .map(([n, name]) => (
              <div key={n || "_"}>
                <div className="tnode">
                  {name}
                  {n && byId[n] && <NodeStatusTag status={byId[n]!.status} />}
                  {n && (
                    <Link className="sm" style={{ fontWeight: 500 }} href={`/flows/${p.id}/n/${n}`}>
                      打开节点
                    </Link>
                  )}
                </div>
                {rows
                  .filter(({ e }) => (e.node_id ?? "") === n)
                  .map(({ e, d }) => (
                    <div key={e.seq} className="tev">
                      <span className={`dot2 k-${d.kind}`} />
                      <div className="row" style={{ alignItems: "flex-start" }}>
                        <span className="xs faint" style={{ width: 84, flex: "none", marginTop: 2 }}>
                          {dt(e.at)}
                        </span>
                        <span className="sm">{d.text}</span>
                      </div>
                    </div>
                  ))}
              </div>
            ))}
          {!rows.length && <div className="sm muted">没有这类事件</div>}
        </div>
      </div>
      <div className="xs faint" style={{ marginTop: 10 }}>
        每轮评审记的是「这一轮提交了哪些文件」和结论；文件本身不留历史版本。对话原文点「打开节点」看
      </div>
    </Page>
  );
}
