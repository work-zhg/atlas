/**
 * 流程事件（记录、概览的「最近」）→ 一句人话。事件由后端 process/service.py、collab/ 记下。
 *
 * ★ 只留人关心的里程碑：谁开始了、谁下了指令、谁上传、谁提交、谁同意或退回、暂停与继续、终止。
 *   系统内部的步骤（指令投递、开评审、评审通过的汇总）返回 null，不显示。
 * ★ 句子里不带步骤名：记录页按步骤分组、概览的「最近」由调用方在前面补步骤名。
 *
 * kind 决定圆点颜色：start 开始，talk 下指令、上传，review 提交，pass 同意、完成，reject 退回、终止。
 */
import { STAGE } from "./format";
import type { ProcessEvent } from "./types";

export type EventKind = "start" | "talk" | "review" | "pass" | "reject";

export interface Milestone {
  text: string;
  kind: EventKind;
  /** 只看评审时留下的 */
  review: boolean;
}

const quote = (v: unknown) => (v ? `：「${String(v)}」` : "");

export function describe(
  ev: ProcessEvent,
  who: (id: string | null) => string,
  nodeName: (id: string | null | undefined) => string,
): Milestone | null {
  const d = ev.data as Record<string, unknown>;
  const actor = who(ev.actor);
  const stage = STAGE[(d.stage as "admit" | "exit") ?? "exit"] ?? "";
  const m = (text: string, kind: EventKind, review = false): Milestone => ({ text, kind, review });
  switch (ev.type) {
    case "process.started":
      return m(`${actor} 发起了流程`, "start");
    case "node.started": {
      const round = Number(d.round ?? 1);
      if (ev.actor && round <= 1) return m(`${actor} 开始了这一步`, "start");
      return m(round > 1 ? `开始第 ${round} 轮` : "开始了", "start");
    }
    case "message.sent":
      return m(`${actor} 给数字员工下了指令${quote(d.text)}`, "talk");
    case "space.uploaded":
      return m(`${actor} 上传了 ${String(d.path ?? "")}`, "talk");
    case "submission.created": {
      const paths = (d.paths as string[] | undefined) ?? [];
      return m(`${actor} 提交评审（第 ${d.round ?? 1} 轮）：${paths.join("、")}`, "review", true);
    }
    case "vote.cast":
      // 不同意的那一票由 review.rejected 说（带退回到哪）
      return d.decision === "approve" ? m(`${actor} 同意${stage}${quote(d.comment)}`, "pass", true) : null;
    case "review.rejected":
      if (d.return_to) return m(`${actor} 没让它开始，退回了上一步「${nodeName(String(d.return_to))}」${quote(d.comment)}`, "reject", true);
      return m(`${actor} 退回修改${quote(d.comment)}`, "reject", true);
    case "node.exited":
      return m("已准出，等下一步接收", "pass", true);
    case "node.passed":
      return m("这一步完成了", "pass", true);
    case "node.returned":
      return m(`被下一步「${nodeName(String(d.by ?? ""))}」退回，要重做${quote(d.comment)}`, "reject", true);
    case "node.taken_over":
      return m(`${actor} 暂停了数字员工，自己改文件`, "talk");
    case "node.handed_back":
      return m(`${actor} 让数字员工继续${quote(d.note)}`, "talk");
    case "process.completed":
      return m("流程完成了", "pass");
    case "process.terminated":
      return m(`${actor} 终止了流程${quote(d.reason)}`, "reject");
    default:
      // instruction.sent、review.opened、review.approved：系统内部的步骤
      return null;
  }
}
