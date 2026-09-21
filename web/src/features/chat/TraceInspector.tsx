"use client";

import { Empty, Tabs } from "antd";

import { Icon } from "@/components/Icon";
import { compactNumber } from "@/lib/format";
import type { RunState } from "@/lib/run-reducer";
import styles from "./chat.module.css";
import { TodoCard } from "./TodoCard";
import { ToolCallRow } from "./ToolCallRow";

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.metric}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

/**
 * 运行轨迹检查器。三个 Tab 的数据源分别是：
 *   计划 ← todos.updated / 工具 ← tool.* / 文件 ← file.written
 */
export function TraceInspector({ state }: { state: RunState }) {
  const { todos, toolCalls, files, usage, meta } = state;
  const liveFiles = files.filter((f) => !f.deleted);

  const items = [
    {
      key: "plan",
      label: (
        <span style={{ display: "flex", alignItems: "center", gap: 5 }}>
          <Icon name="todo" size={14} />
          计划
          {todos.length > 0 && <span className={styles.toolCost}>{todos.length}</span>}
        </span>
      ),
      children:
        todos.length > 0 ? (
          <TodoCard todos={todos} />
        ) : (
          <Empty description="本轮没有产生计划" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ),
    },
    {
      key: "tools",
      label: (
        <span style={{ display: "flex", alignItems: "center", gap: 5 }}>
          <Icon name="wrench" size={14} />
          工具
          {toolCalls.length > 0 && <span className={styles.toolCost}>{toolCalls.length}</span>}
        </span>
      ),
      children:
        toolCalls.length > 0 ? (
          <div>
            {toolCalls.map((c) => (
              <ToolCallRow key={c.callId} call={c} />
            ))}
          </div>
        ) : (
          <Empty description="本轮没有工具调用" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ),
    },
    {
      key: "files",
      label: (
        <span style={{ display: "flex", alignItems: "center", gap: 5 }}>
          <Icon name="folder" size={14} />
          文件
          {liveFiles.length > 0 && <span className={styles.toolCost}>{liveFiles.length}</span>}
        </span>
      ),
      children:
        liveFiles.length > 0 ? (
          <div>
            <p className={styles.insSection}>虚拟文件系统</p>
            {liveFiles.map((f) => (
              <div key={f.path} className={styles.fileRow}>
                <Icon name="file" size={14} style={{ color: "var(--fg-3)" }} />
                <span className={styles.fileName} title={f.path}>
                  {f.path}
                </span>
                {f.sizeBytes !== undefined && (
                  <span className={styles.fileSize}>{compactNumber(f.sizeBytes)}B</span>
                )}
              </div>
            ))}
          </div>
        ) : (
          <Empty description="本轮没有文件产出" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ),
    },
  ];

  return (
    <aside className={`${styles.pane} ${styles.inspector}`} aria-label="运行轨迹">
      <Tabs
        items={items}
        size="small"
        style={{ flex: 1, minHeight: 0 }}
        tabBarStyle={{ margin: 0, padding: "0 var(--s-4)" }}
      />

      {/* 本轮消耗 —— 决策 4：不展示成本，只展示 token */}
      <div style={{ borderTop: "1px solid var(--line)", padding: "var(--s-4)" }}>
        <p className={styles.insSection}>本轮消耗</p>
        <dl style={{ margin: 0 }}>
          <Metric label="输入" value={compactNumber(usage.inputTokens)} />
          <Metric label="输出" value={compactNumber(usage.outputTokens)} />
          {usage.cacheRead !== undefined && usage.cacheRead > 0 && (
            <Metric label="缓存命中" value={compactNumber(usage.cacheRead)} />
          )}
          <Metric label="合计" value={compactNumber(usage.totalTokens)} />
          {meta && <Metric label="模型" value={meta.model} />}
          {usage.thinkingOccurred && <Metric label="思考" value="已触发" />}
        </dl>
      </div>
    </aside>
  );
}
