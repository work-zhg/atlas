"use client";

/**
 * 编辑器里的「知识库」一节（doc/atlas-rag-design.html §10）。
 *
 * ★ 绑定不是授权：运行时知识库按这一轮的权限主体（X-Auth-Uid）判 —— 以人为权限是使用者本人，
 *   以 Agent 为权限是它的数字员工。读不到的库跳过并提示（同 MCP）。
 * ★ 能选的库 = 权限主体能读的（Atlas /v1/knowledge-bases）；保存时服务端再校验一次新绑定的库。
 */
import { Alert, InputNumber, Radio, Select, Space, Switch, Tag } from "antd";
import Link from "next/link";

import { useKnowledgeBases } from "@/api/meta";
import type { AgentSpecIn } from "@/api/types";

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
  agentId,
  authSubject,
  onChange,
}: {
  value: Knowledge;
  kind: string;
  readOnly: boolean;
  /** 编辑已有的 Agent：按它的权限主体列库；新建时按 authSubject */
  agentId?: string;
  authSubject: "user" | "agent";
  onChange: (v: Knowledge) => void;
}) {
  const datasets = useKnowledgeBases({ agentId, authSubject });
  const subject = datasets.data?.subject;
  const acting = subject ? subject.kind === "agent" : authSubject === "agent";
  const options = (datasets.data?.data ?? []).map((d) => ({
    value: d.id,
    label: `${d.icon ?? "📚"} ${d.name}`,
  }));
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
      {subject && (
        <div style={{ marginBottom: 8, fontSize: 12, color: "var(--fg-2)" }}>
          <div>可选的知识库按{acting ? "这个 Agent 的数字员工" : "你"}的权限列出</div>
          {subject.notes.map((n) => (
            <div key={n} style={{ marginTop: 4 }}>
              ⚠ {n}
            </div>
          ))}
        </div>
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
          // 已绑定、但权限主体读不到的库：保留，不让一次保存把它悄悄去掉（运行时跳过并提示）
          ...ids.filter((id) => !known.has(id)).map((id) => ({ value: id, label: `🔒 ${id.slice(0, 8)}…` })),
        ]}
      />
      {ids.length > 0 && (
        <>
          <Alert
            type="info"
            showIcon
            style={{ margin: "10px 0" }}
            message={
              acting
                ? "按数字员工的权限检索：能使用这个智能体的人，都可以通过它读到数字员工能读的库。"
                : "按使用者自己的权限检索：使用者读不到的库，运行时会跳过并提示。"
            }
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
