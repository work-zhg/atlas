"use client";

/**
 * 记忆写入的当场提示（doc/memory-design.html §4.7）：「已记住 #131［撤销］」「已遗忘 #96［撤销］」。
 *
 * ★ 智能体在对话中保存记忆不走审批，换来的是每次写入用户当场看得见、一键能撤销：撤销把这次写入
 *   反向执行（删除新条目，恢复被它取代的旧条目）。撤销状态只在本页记着；刷新后再点一次，
 *   后端会说「已经撤销过了」。
 */
import { useState } from "react";

import { useAtlasHttp } from "../client";
import { Icon } from "../components/Icon";
import type { MemoryArtifact } from "../lib/events";
import styles from "./chat.module.css";

type UndoState = { kind: "idle" } | { kind: "busy" } | { kind: "undone" } | { kind: "failed"; message: string };

export function MemoryNotice({ memory }: { memory: MemoryArtifact }) {
  const http = useAtlasHttp();
  const [state, setState] = useState<UndoState>({ kind: "idle" });
  const saved = memory.action === "saved";
  const removed = memory.removed.map((n) => `#${n}`).join("、");

  async function undo() {
    if (!memory.revision) return;
    setState({ kind: "busy" });
    try {
      await http.post(`/v1/memories/revisions/${memory.revision}/undo`);
      setState({ kind: "undone" });
    } catch (e) {
      setState({ kind: "failed", message: (e as Error).message });
    }
  }

  return (
    <div className={styles.memoryNotice} role="status">
      <Icon name="brain" size={14} className={styles.memoryIcon} />
      <span className={styles.memoryText}>
        {saved ? `已记住 #${memory.number}` : `已遗忘 ${removed}`}
        {saved && memory.content ? <span className={styles.memoryContent}>「{memory.content}」</span> : null}
      </span>
      {saved && memory.removed.length > 0 && <span className={styles.memoryMeta}>取代了 {removed}</span>}
      {memory.topic && <span className={styles.memoryMeta}>{memory.topic}</span>}
      {memory.revision && state.kind !== "undone" && (
        <button type="button" className={styles.memoryUndo} onClick={undo} disabled={state.kind === "busy"}>
          {state.kind === "busy" ? "撤销中…" : "撤销"}
        </button>
      )}
      {state.kind === "undone" && <span className={styles.memoryMeta}>已撤销</span>}
      {state.kind === "failed" && <span className={styles.memoryError}>{state.message}</span>}
    </div>
  );
}
