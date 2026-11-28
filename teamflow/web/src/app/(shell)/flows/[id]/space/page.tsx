"use client";

/**
 * 流程 · 文件：这条流程共用的文件夹（后端叫流程空间）。左边是文件夹：全部、资料（成员上传）、每一步一个；
 * 右边是文件：状态（已交付 / 评审中 / 草稿）、谁写的、修改时间。点文件在新标签页打开。
 *
 * ★ 文件不做版本管理：只有当前内容；评审记录记的是「这一轮交了哪些文件」。
 * ★ 每一步的数字员工按约定把文件写在「步骤名/」下；成员上传默认放进「资料/」，数字员工开工时会收到清单。
 */
import { Button, Table, Upload } from "antd";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";

import { FlowHeader } from "@/components/FlowHeader";
import { AgentChip, Empty, Icon, LoadError, Loading, Page, StepNo, viewHref, Who } from "@/components/ui";
import { ago, MATERIALS_DIR, size } from "@/lib/format";
import { useFlow, useUpload } from "@/lib/flow";
import { useSpace } from "@/lib/queries";
import { nodeIds, stepNos } from "@/lib/structure";
import type { SpaceEntry } from "@/lib/types";

export default function FilesPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const dir = useSearchParams().get("dir") ?? "";
  const flow = useFlow(id);
  const { proc, nameOf, agentOf, version } = flow;
  const space = useSpace(id, dir);
  const root = useSpace(id, "");
  const upload = useUpload(id);
  const setDir = (d: string) => router.replace(`/flows/${id}/space${d ? `?dir=${encodeURIComponent(d)}` : ""}`);

  if (proc.error) return <LoadError error={proc.error} />;
  if (!proc.data) return <Loading />;
  const p = proc.data;
  const s = p.structure;
  const no = stepNos(s);
  const canWrite = p.can_run && p.status === "running";
  const byName = Object.fromEntries(p.nodes.map((n) => [n.name, n]));
  const exists = new Set((root.data?.entries ?? []).filter((e) => e.is_dir).map((e) => e.path));
  // 文件 → 哪一步最近一轮交付了它、那一步现在的状态
  const delivered: Record<string, { round: number; reviewing: boolean }> = {};
  for (const n of p.nodes)
    for (const x of n.latest_submission?.paths ?? [])
      delivered[x] = { round: n.latest_submission!.round, reviewing: n.status === "exit_review" || n.status === "admit_review" };
  const top = dir.split("/")[0] ?? "";
  const parts = dir ? dir.split("/") : [];

  const folders: { key: string; label: React.ReactNode; empty: boolean }[] = [
    { key: "", label: <><Icon name="layers" size={14} /> 全部</>, empty: false },
    { key: MATERIALS_DIR, label: <><Icon name="folder" size={14} /> 资料</>, empty: !exists.has(MATERIALS_DIR) },
    ...nodeIds(s).map((nid) => {
      const n = p.nodes.find((x) => x.id === nid)!;
      return { key: n.name, label: <><StepNo n={no[nid]} status={n.status} /> {n.name}</>, empty: !exists.has(n.name) };
    }),
  ];

  const author = (e: SpaceEntry) => {
    if (e.uploaded_by) return <span className="sm"><Who id={e.uploaded_by} name={nameOf(e.uploaded_by)} size={16} /> <span className="xs faint">上传</span></span>;
    const n = byName[e.path.split("/")[0] ?? ""];
    const agent = n ? agentOf(n) : null;
    return agent ? <span className="sm"><AgentChip name={agent} size={16} /></span> : <span className="xs faint">—</span>;
  };
  const columns = [
    {
      title: "文件",
      render: (_: unknown, e: SpaceEntry) =>
        e.is_dir ? (
          <a className="row" style={{ gap: 6 }} onClick={() => setDir(e.path)}>
            <Icon name="folder" size={15} />
            {e.name}
          </a>
        ) : (
          <Link className="row" style={{ gap: 6, color: "var(--fg)" }} href={viewHref(p.id, e.path)} target="_blank" rel="noopener" title="在新标签页打开">
            <Icon name="file" size={15} />
            <span className="ellipsis">{e.name}</span>
          </Link>
        ),
    },
    {
      title: "状态",
      width: 140,
      render: (_: unknown, e: SpaceEntry) => {
        if (e.is_dir) return null;
        const d = delivered[e.path];
        if (!d) return <span className="rtag">草稿</span>;
        return d.reviewing ? (
          <span className="rtag exit">评审中 · 第 {d.round} 轮</span>
        ) : (
          <span className="rtag" style={{ background: "var(--success-soft)", color: "var(--success)" }}>
            已交付 · 第 {d.round} 轮
          </span>
        );
      },
    },
    { title: "谁写的", width: 170, render: (_: unknown, e: SpaceEntry) => (e.is_dir ? null : author(e)) },
    { title: "修改", width: 100, render: (_: unknown, e: SpaceEntry) => <span className="xs muted">{e.is_dir ? "" : ago(e.modified_at)}</span> },
    { title: "大小", width: 80, render: (_: unknown, e: SpaceEntry) => <span className="xs muted">{e.is_dir ? "" : size(e.size)}</span> },
  ];
  const target = dir || MATERIALS_DIR;

  return (
    <Page>
      <FlowHeader process={p} tab="files" version={version} nameOf={nameOf} />
      {p.status !== "running" && (
        <div className="banner info" style={{ marginBottom: 12 }}>
          <Icon name="lock" size={15} />
          <div>流程{p.status === "completed" ? "已完成" : "已终止"}：文件只能看、不能改</div>
        </div>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "210px minmax(0,1fr)", gap: 14, alignItems: "start" }}>
        <div className="card" style={{ padding: 6 }}>
          {folders.map((f) => (
            <div
              key={f.key || "_all"}
              className={`folder ${f.key === top ? "on" : ""} ${f.empty ? "empty" : ""}`}
              onClick={() => setDir(f.key)}
            >
              <span className="row grow" style={{ gap: 6 }}>
                {f.label}
              </span>
              {f.empty && <span className="xs">空</span>}
            </div>
          ))}
          {canWrite && (
            <Upload
              multiple
              showUploadList={false}
              customRequest={({ file, onSuccess }) => void upload(file as File, target).then(() => onSuccess?.({}))}
            >
              <Button block icon={<Icon name="upload" size={14} />} style={{ marginTop: 6 }} title={`上传到「${target}」`}>
                上传文件
              </Button>
            </Upload>
          )}
        </div>
        <div>
          <div className="row sm" style={{ marginBottom: 8, minHeight: 22 }}>
            <a onClick={() => setDir("")}>全部</a>
            {parts.map((seg, i) => (
              <span key={i} className="row" style={{ gap: 4 }}>
                <span className="faint">/</span>
                <a onClick={() => setDir(parts.slice(0, i + 1).join("/"))}>{seg}</a>
              </span>
            ))}
            {space.data?.truncated && <span className="xs warn-text">文件太多，只列了一部分</span>}
            <span className="sp" />
            {canWrite && <span className="xs faint">上传的文件放进「{target}」</span>}
          </div>
          {space.error ? (
            <LoadError error={space.error} onRetry={() => void space.refetch()} />
          ) : space.data && !space.data.entries.length ? (
            canWrite ? (
              <Upload.Dragger
                multiple
                showUploadList={false}
                customRequest={({ file, onSuccess }) => void upload(file as File, target).then(() => onSuccess?.({}))}
              >
                <Empty icon="folder" title={dir ? "这个文件夹还是空的" : "还没有文件"}>
                  把文件拖到这里，或点一下选择文件（放进「{target}」）
                </Empty>
              </Upload.Dragger>
            ) : (
              <div className="card">
                <Empty icon="folder" title={dir ? "这个文件夹还是空的" : "还没有文件"} />
              </div>
            )
          ) : (
            <Table rowKey="path" size="middle" loading={space.isPending} columns={columns} dataSource={space.data?.entries ?? []} pagination={false} />
          )}
        </div>
      </div>
    </Page>
  );
}
