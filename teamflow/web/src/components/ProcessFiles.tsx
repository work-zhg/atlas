"use client";

/**
 * 流程目录预览（产物仓库与 Agent 提交 · §8）：本流程在产物仓库里的目录。
 * ★ TeamFlow 不跟踪提交：这里看的是仓库实时内容（master），或某次评审锁定的 commit。
 * ★ 后端只开放本流程目录；成员不需要绑定 Gitee 账号也能看。
 */
import { useQuery } from "@tanstack/react-query";
import { Alert, Col, Empty, Row, Select, Space, Spin, Tag, Tree } from "antd";
import type { DataNode } from "antd/es/tree";
import { useMemo, useState } from "react";

import { api, ApiError, errorText } from "@/lib/api";
import { dt, size } from "@/lib/format";
import type { NodeDetail } from "@/lib/types";

interface FileList {
  ref: string;
  root: string;
  files: { path: string; size: number }[];
  truncated: boolean;
}

/** 平铺路径 → 目录树（相对流程目录） */
function toTree(root: string, files: FileList["files"]): DataNode[] {
  const top: DataNode[] = [];
  const dirs = new Map<string, DataNode[]>([["", top]]);
  for (const f of files) {
    const parts = f.path.slice(root.length).split("/");
    const name = parts.pop() ?? f.path;
    let parent = "";
    for (const seg of parts) {
      const dir = parent ? `${parent}/${seg}` : seg;
      if (!dirs.has(dir)) {
        const children: DataNode[] = [];
        dirs.get(parent)!.push({ key: `dir:${dir}`, title: seg, children, selectable: false });
        dirs.set(dir, children);
      }
      parent = dir;
    }
    dirs.get(parent)!.push({
      key: f.path,
      isLeaf: true,
      title: (
        <span>
          {name} <span className="tf-muted" style={{ fontSize: 11 }}>{size(f.size)}</span>
        </span>
      ),
    });
  }
  return top;
}

export function ProcessFiles({ processId, node }: { processId: string; node: NodeDetail }) {
  const [ref, setRef] = useState("master");
  const [picked, setPicked] = useState<string>();
  const locked = node.artifacts.find((a) => a.commit === ref);

  const list = useQuery({
    queryKey: ["process-files", processId, ref],
    queryFn: () => api.get<FileList>(`/processes/${processId}/files`, { ref }),
  });
  const files = list.data?.files ?? [];
  // 默认打开本节点产物；锁定版本下打开那次评审的产物文件
  const fallback = locked?.path ?? node.artifact_path ?? undefined;
  const path = picked && files.some((f) => f.path === picked) ? picked : files.some((f) => f.path === fallback) ? fallback : undefined;
  const content = useQuery({
    queryKey: ["process-file", processId, list.data?.ref, path],
    queryFn: () => api.get<{ content: string }>(`/processes/${processId}/files/content`, { path: path!, ref: list.data!.ref }),
    enabled: !!path && !!list.data,
  });
  const tree = useMemo(() => (list.data ? toTree(list.data.root, list.data.files) : []), [list.data]);

  const notCommitted = list.error instanceof ApiError && list.error.code === "REF_NOT_FOUND";
  return (
    <Space direction="vertical" size={10} style={{ width: "100%" }}>
      <Space wrap>
        <Select
          style={{ minWidth: 300 }}
          value={ref}
          onChange={(v) => {
            setRef(v);
            setPicked(undefined);
          }}
          options={[
            { value: "master", label: "仓库最新（master）" },
            ...node.artifacts.map((a) => ({
              value: a.commit,
              label: `v${a.version} · 第 ${a.round} 轮评审锁定 · ${a.commit.slice(0, 8)}`,
            })),
          ]}
        />
        {node.repository && (
          <a href={node.repository.web_url} target="_blank" rel="noreferrer">
            {node.repository.full_name} ↗
          </a>
        )}
      </Space>
      {locked && (
        <div className="tf-muted" style={{ fontSize: 12.5 }}>
          <Tag color={locked.source === "unknown" ? "orange" : undefined}>
            {locked.source === "agent" ? "Agent" : locked.source === "user" ? (locked.by ?? locked.author ?? "成员") : `未知：${locked.author ?? "?"}`}
          </Tag>
          {locked.note && <span>{locked.note} · </span>}
          {dt(locked.committed_at ?? locked.created_at)}
          {locked.url && (
            <a href={locked.url} target="_blank" rel="noreferrer" style={{ marginLeft: 8 }}>
              在 Gitee 查看 ↗
            </a>
          )}
        </div>
      )}
      {node.artifact_path && (
        <div className="tf-muted" style={{ fontSize: 12.5 }}>
          本节点产物：<code className="tf-mono">{node.artifact_path}</code>。Agent 用 git 提交到这里；提交准出评审时锁定当时的最新提交。
        </div>
      )}
      {notCommitted ? (
        <Empty description="仓库里还没有提交：等 Agent 提交产物" />
      ) : list.error ? (
        <Alert type="error" showIcon message={errorText(list.error)} />
      ) : list.isPending ? (
        <Spin />
      ) : !files.length ? (
        <Empty description="本流程目录下还没有文件" />
      ) : (
        <Row gutter={12}>
          <Col span={8}>
            <Tree
              showLine
              defaultExpandAll
              selectedKeys={path ? [path] : []}
              onSelect={(keys) => keys[0] && setPicked(String(keys[0]))}
              treeData={tree}
              style={{ background: "transparent" }}
            />
            {list.data?.truncated && <div className="tf-muted" style={{ fontSize: 12 }}>文件太多，只列出了一部分</div>}
          </Col>
          <Col span={16}>
            {!path ? (
              <Empty description="选择文件查看" />
            ) : content.error ? (
              <Alert type="warning" showIcon message={errorText(content.error)} />
            ) : content.data ? (
              <>
                <div className="tf-muted tf-mono" style={{ fontSize: 11.5, marginBottom: 6 }}>
                  {path} @ {list.data!.ref.slice(0, 10)}
                </div>
                <pre className="tf-md">{content.data.content}</pre>
              </>
            ) : (
              <Spin />
            )}
          </Col>
        </Row>
      )}
    </Space>
  );
}
