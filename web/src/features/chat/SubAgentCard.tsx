"use client";

import { Tag } from "antd";

import { Icon } from "@/components/Icon";
import { durationMs } from "@/lib/format";
import type { SubagentRun } from "@/lib/run-reducer";
import { avatarBackground, avatarLetter } from "@/features/agents/avatar";
import styles from "./chat.module.css";

const STATUS: Record<SubagentRun["status"], { color: string; label: string }> = {
  running: { color: "processing", label: "执行中" },
  ok: { color: "success", label: "已完成" },
  error: { color: "error", label: "失败" },
};

/**
 * 子智能体委派块。
 *
 * ★ 原型里画的是「步骤列表」，这里换成「任务 + 返回结果」——
 *   不是偷懒：LangGraph 子图的命名空间是 `tools:<task id>`，与主 agent 那次
 *   task 调用的 tool_call_id **没有可靠映射**，而内核鼓励并发委派
 *   多个子智能体。按到达顺序猜关联在并发时会串到别人的卡片上，
 *   宁可不显示也不能显示错的。详见 docs/trace-event.md。
 *
 * ★ 上面那段论证针对的是**图内子图**的实现方式，它已经不存在了 ——
 *   子智能体现在跑在自己的子 run 里（doc/detail/subagent.html）。
 *   `depth` 的语义随之改成「run 在委派树里的深度」：子 run 产出的事件带
 *   depth=1，与 tool_call_id 无关，因此配对是可靠的。
 *
 *   步骤列表因此重新可做，但要等 thread 级事件流上线（前端目前只订阅父 run
 *   那一条流，看不到子 run 的过程）—— 见 doc/detail/suspension.html §04。
 */
export function SubAgentCard({ run }: { run: SubagentRun }) {
  const status = STATUS[run.status];

  return (
    <details className={styles.subagent} open={run.status === "running"}>
      <summary>
        <Icon name="right" size={12} className={styles.caret} />
        <span
          className={styles.agentAvatar}
          style={{
            width: 18,
            height: 18,
            fontSize: 9,
            borderRadius: 4,
            background: avatarBackground("research"),
          }}
          aria-hidden="true"
        >
          {avatarLetter(run.name)}
        </span>
        <span className={styles.saName}>{run.name}</span>
        <Tag color={status.color} style={{ marginInlineEnd: 0 }}>
          {status.label}
        </Tag>
        <span className={styles.toolCost} style={{ marginLeft: "auto" }}>
          {run.status === "running" ? "执行中" : durationMs(run.durationMs)}
        </span>
      </summary>

      <div className={styles.saBody}>
        <p className={styles.saLabel}>委派任务</p>
        <p className={styles.saText}>{run.task || "（未提供任务描述）"}</p>

        {run.result && (
          <>
            <p className={styles.saLabel}>返回结果</p>
            <p className={styles.saText}>{run.result}</p>
          </>
        )}
      </div>
    </details>
  );
}
