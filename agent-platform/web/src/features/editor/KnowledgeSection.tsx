"use client";

/**
 * 编辑器里的「知识库」一节（doc/atlas-rag-design.html §10、§11.2）。
 *
 * ★ 绑定 = 间接授权：能用这个智能体的人都能经它读到库的内容 —— 绑定前要说清楚。
 * ★ 能选的库来自知识库服务（当前用户可读的）；保存时服务端还会再校验一次新绑定的库。
 */
import { Alert, InputNumber, Radio, Select, Space, Switch, Tag } from "antd";
import Link from "next/link";

import type { AgentSpecIn } from "@/api/types";
import { useDatasets } from "@/features/knowledge/api";

import styles from "./editor.module.css";

type Knowledge = NonNullable<AgentSpecIn["knowledge"]>;

export const DEFAULT_KNOWLEDGE: Knowledge = {
  dataset_ids: [],
  mode: "auto",
  top_k: 4,
  score_threshold: null,
  rerank: false,
};

export function KnowledgeSection({
  value,
  kind,
  readOnly,
  onChange,
}: {
  value: Knowledge;
  kind: string;
  readOnly: boolean;
  onChange: (v: Knowledge) => void;
}) {
  const datasets = useDatasets();
  const options = (datasets.data?.data ?? []).map((d) => ({ value: d.id, label: `${d.icon} ${d.name}` }));
  const known = new Set(options.map((o) => o.value));
  const ids = value.dataset_ids ?? [];
  const acp = kind === "acp";
  const set = (patch: Partial<Knowledge>) => onChange({ ...value, ...patch });

  return (
    <div id="e-knowledge" className={styles.section}>
      <h2 className={styles.sectionTitle}>知识库</h2>
      <p className={styles.sectionDesc}>
        绑定后，智能体回答时检索这些库里的内容并注明出处。在 <Link href="/knowledge">知识库</Link> 管理文档。
      </p>
      {datasets.error && (
        <Alert type="warning" showIcon message={`读取知识库列表失败：${(datasets.error as Error).message}`} />
      )}
      <Select
        mode="multiple"
        style={{ width: "100%" }}
        placeholder="选择知识库（最多 10 个）"
        disabled={readOnly}
        loading={datasets.isPending}
        value={ids}
        maxCount={10}
        onChange={(dataset_ids: string[]) => set({ dataset_ids })}
        options={[
          ...options,
          // 已绑定但当前用户看不到的库：保留，不让一次保存把别人绑的库悄悄去掉
          ...ids.filter((id) => !known.has(id)).map((id) => ({ value: id, label: `🔒 ${id.slice(0, 8)}…` })),
        ]}
      />
      {ids.length > 0 && (
        <>
          <Alert
            type="info"
            showIcon
            style={{ margin: "10px 0" }}
            message="能使用这个智能体的人，都可以通过它读到所绑定知识库的内容。"
          />
          <Space direction="vertical" style={{ width: "100%" }}>
            <Radio.Group
              disabled={readOnly}
              value={acp ? "tool" : value.mode}
              onChange={(e) => set({ mode: e.target.value })}
            >
              <Radio value="auto" disabled={acp}>
                自动检索：每轮先按用户的问题检索，资料并入本轮输入
              </Radio>
              <Radio value="tool">工具：由模型决定何时调用 knowledge_search</Radio>
            </Radio.Group>
            {acp && <Tag>外部 CLI 只能以工具方式（MCP）接入知识库</Tag>}
            <Space wrap>
              <span>Top K</span>
              <InputNumber
                min={1}
                max={20}
                disabled={readOnly}
                value={value.top_k}
                onChange={(v) => set({ top_k: v ?? 4 })}
              />
              <span style={{ marginLeft: 12 }}>分数阈值</span>
              <Switch
                size="small"
                disabled={readOnly}
                checked={value.score_threshold != null}
                onChange={(on) => set({ score_threshold: on ? 0.5 : null })}
              />
              {value.score_threshold != null && (
                <InputNumber
                  min={0}
                  max={1}
                  step={0.05}
                  disabled={readOnly}
                  value={value.score_threshold}
                  onChange={(v) => set({ score_threshold: v ?? 0.5 })}
                />
              )}
              {ids.length > 1 && (
                <>
                  <span style={{ marginLeft: 12 }}>多库统一重排</span>
                  <Switch size="small" disabled={readOnly} checked={value.rerank} onChange={(rerank) => set({ rerank })} />
                </>
              )}
            </Space>
          </Space>
        </>
      )}
    </div>
  );
}
