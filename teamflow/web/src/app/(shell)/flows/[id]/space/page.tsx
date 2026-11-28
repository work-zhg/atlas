"use client";

/**
 * 流程空间：向 Agent 平台申请的工作空间，这条流程所有节点的数字员工会话都挂载它。
 *
 * ★ 「提交」列：哪个节点最近一轮把它选为产出物；没被选过的是过程文件（草稿、中间结果）。
 * ★ 产物不做版本管理：这里只有文件的当前内容。读写成员能上传（流程进行中）。
 */
import { useQueryClient } from "@tanstack/react-query";
import { App, Button, Table, Upload } from "antd";
import { useParams } from "next/navigation";
import { useState } from "react";

import { downloadUrl, FilePreview } from "@/components/FilePreview";
import { Crumb, Empty, Icon, LoadError, Loading, Page, PageHead, Who } from "@/components/ui";
import { errorText, putFile } from "@/lib/api";
import { ago, size } from "@/lib/format";
import { useFlow } from "@/lib/flow";
import { useProject, useSpace } from "@/lib/queries";
import { nodeIds } from "@/lib/structure";
import type { SpaceEntry } from "@/lib/types";

export default function SpacePage() {
  const { id } = useParams<{ id: string }>();
  const { message } = App.useApp();
  const qc = useQueryClient();
  // 上传人记在 space.uploaded 事件里，useFlow 已经按事件取了人名
  const { proc, nameOf } = useFlow(id);
  const project = useProject(proc.data?.project_id);
  const [dir, setDir] = useState("");
  const space = useSpace(id, dir);
  const [pick, setPick] = useState<string | null>(null);

  if (proc.error) return <LoadError error={proc.error} />;
  if (!proc.data) return <Loading />;
  const p = proc.data;
  const canWrite = p.can_run && p.status === "running";
  // 节点名 → 节点 id（目录名就是节点名）
  const nodeByName = Object.fromEntries(nodeIds(p.structure).map((n) => [p.structure.nodes[n]!.name, n]));
  const submittedBy: Record<string, { node: string; round: number }[]> = {};
  for (const n of p.nodes)
    for (const x of n.latest_submission?.paths ?? []) (submittedBy[x] ??= []).push({ node: n.name, round: n.latest_submission!.round });
  const parts = dir ? dir.split("/") : [];

  const upload = async (file: File) => {
    const path = dir ? `${dir}/${file.name}` : file.name;
    try {
      await putFile(`/processes/${p.id}/space`, file, { path });
      message.success(`已上传 ${path}`);
      await qc.invalidateQueries({ queryKey: ["process", p.id, "space"] });
    } catch (e) {
      message.error(errorText(e));
    }
  };

  const columns = [
    {
      title: "文件",
      render: (_: unknown, e: SpaceEntry) => (
        <span className="row" style={{ gap: 6 }}>
          <Icon name={e.is_dir ? "folder" : "file"} size={15} />
          <span className="mono">{e.name}</span>
        </span>
      ),
    },
    {
      title: "提交",
      width: 170,
      render: (_: unknown, e: SpaceEntry) =>
        submittedBy[e.path]?.map((x) => (
          <span key={x.node} className="rtag" style={{ background: "var(--success-soft)", color: "var(--success)", marginRight: 4 }}>
            {x.node} · 第 {x.round} 轮
          </span>
        )) ?? (e.is_dir ? null : <span className="xs faint">过程文件</span>),
    },
    {
      title: "来源",
      width: 190,
      render: (_: unknown, e: SpaceEntry) =>
        e.is_dir ? null : e.uploaded_by ? (
          <span className="sm">
            <Who id={e.uploaded_by} name={nameOf(e.uploaded_by)} size={15} /> <span className="xs faint">上传</span>
          </span>
        ) : e.node && nodeByName[e.node] ? (
          <span className="xs muted">「{e.node}」的会话里写的</span>
        ) : (
          <span className="xs faint">—</span>
        ),
    },
    { title: "修改", width: 100, render: (_: unknown, e: SpaceEntry) => <span className="xs muted">{e.is_dir ? "" : ago(e.modified_at)}</span> },
    { title: "大小", width: 80, render: (_: unknown, e: SpaceEntry) => <span className="xs muted">{e.is_dir ? "" : size(e.size)}</span> },
  ];

  return (
    <Page>
      <Crumb
        items={[
          [project.data?.name ?? "项目", `/projects/${p.project_id}`],
          [`${p.code} ${p.title}`, `/flows/${p.id}`],
          ["流程空间", undefined],
        ]}
      />
      <PageHead
        title={`流程空间 · ${p.title}`}
        sub={
          <>
            <Icon name="folder" size={13} /> Agent 平台的应用工作空间 <span className="mono xs">{p.space_id.slice(0, 8)}</span>
            <span>·</span>这条流程所有节点的数字员工会话都挂载它（/space）
          </>
        }
        extra={
          canWrite && (
            <Upload
              multiple
              showUploadList={false}
              customRequest={({ file, onSuccess }) => {
                void upload(file as File).then(() => onSuccess?.({}));
              }}
            >
              <Button icon={<Icon name="upload" size={14} />}>上传到{dir ? `「${parts[parts.length - 1]}」` : "根目录"}</Button>
            </Upload>
          )
        }
      />
      {p.status !== "running" && (
        <div className="banner info" style={{ marginBottom: 12 }}>
          <Icon name="lock" size={15} />
          <div>流程{p.status === "completed" ? "已完成" : "已终止"}：流程空间只读保留</div>
        </div>
      )}
      <div className="row sm" style={{ marginBottom: 8 }}>
        <a onClick={() => setDir("")}>流程空间</a>
        {parts.map((seg, i) => (
          <span key={i} className="row" style={{ gap: 4 }}>
            <span className="faint">/</span>
            <a onClick={() => setDir(parts.slice(0, i + 1).join("/"))}>{seg}</a>
          </span>
        ))}
        {space.data?.truncated && <span className="xs warn-text">文件太多，只列了一部分</span>}
      </div>
      {space.error ? (
        <LoadError error={space.error} onRetry={() => void space.refetch()} />
      ) : space.data && !space.data.entries.length && !dir ? (
        <div className="card">
          <Empty icon="folder" title="流程空间还是空的">
            数字员工在节点会话里写的文件、成员上传的资料都会出现在这里
          </Empty>
        </div>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: pick ? "minmax(0,1.5fr) minmax(0,1fr)" : "1fr", gap: 16, alignItems: "start" }}>
          <Table
            className="table-click"
            rowKey="path"
            size="middle"
            loading={space.isPending}
            columns={columns}
            dataSource={space.data?.entries ?? []}
            pagination={false}
            rowClassName={(e) => (e.path === pick ? "ant-table-row-selected" : "")}
            onRow={(e) => ({ onClick: () => (e.is_dir ? (setDir(e.path), setPick(null)) : setPick(e.path)) })}
          />
          {pick && (
            <div>
              <FilePreview processId={p.id} path={pick} label="当前内容" />
              <div className="xs faint" style={{ marginTop: 8 }}>
                产物不做版本管理：这里永远是文件的当前内容 · <a href={downloadUrl(p.id, pick)}>下载</a>
              </div>
            </div>
          )}
        </div>
      )}
      <div className="xs faint" style={{ marginTop: 10 }}>
        目录名就是节点名：每个节点的数字员工按约定把产出写在「节点名/」下；上游提交的文件下游直接读，不拷贝
      </div>
    </Page>
  );
}
