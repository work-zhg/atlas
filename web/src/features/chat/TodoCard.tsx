"use client";

import type { Todo } from "@/lib/events";
import { Icon } from "@/components/Icon";
import styles from "./chat.module.css";

/**
 * 计划卡 —— write_todos 的可视化。
 * 数据来自 todos.updated，是**全量快照**（契约规则 1），直接整表渲染。
 */
export function TodoCard({ todos, title = "执行计划" }: { todos: Todo[]; title?: string }) {
  if (todos.length === 0) return null;

  const done = todos.filter((t) => t.status === "completed").length;

  return (
    <div className={styles.todoCard}>
      <div className={styles.todoCardHead}>
        <Icon name="todo" size={14} />
        {title}
        <span style={{ marginLeft: "auto", fontWeight: 400, color: "var(--fg-3)" }}>
          {done}/{todos.length}
        </span>
      </div>
      <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
        {todos.map((t, i) => (
          <li
            key={t.id ?? i}
            className={[
              styles.todo,
              t.status === "completed" ? styles.todoDone : "",
              t.status === "in_progress" ? styles.todoDoing : "",
            ]
              .filter(Boolean)
              .join(" ")}
          >
            {/* 状态用形状+颜色双重编码，不只靠颜色区分（a11y） */}
            <span className={styles.todoMark} aria-hidden="true" />
            <span className={styles.todoText}>{t.content}</span>
            <span className="sr-only">
              {t.status === "completed" ? "已完成" : t.status === "in_progress" ? "进行中" : "待办"}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
