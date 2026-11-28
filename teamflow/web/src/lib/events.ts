/**
 * 流程事件（追溯、流程详情的「最近」）→ 一句话。事件由后端 process/service.py、collab/ 记下。
 *
 * kind 决定时间线上的圆点颜色：lock 锁定 / 开工，save 写入、上传，review 提交与开评审，pass 通过，
 * reject 驳回、打回、终止。
 */
import { STAGE } from "./format";
import type { ProcessEvent } from "./types";

export type EventKind = "lock" | "save" | "review" | "pass" | "reject" | "other";

const INSTRUCTION = { kickoff: "开工指令", revise: "修改指令", human: "人的指令", handback: "交还说明" } as Record<string, string>;

export function describe(
  ev: ProcessEvent,
  who: (id: string | null) => string,
  nodeName: (id: string | null | undefined) => string,
): { text: string; kind: EventKind } {
  const d = ev.data as Record<string, unknown>;
  const actor = who(ev.actor);
  const node = nodeName(ev.node_id);
  const stage = STAGE[(d.stage as "admit" | "exit") ?? "exit"] ?? "";
  switch (ev.type) {
    case "process.started":
      return { text: `${actor} 发起流程，锁定模板 v${d.version ?? "?"}；流程空间已申请`, kind: "lock" };
    case "node.started":
      return {
        text: `「${node}」开工（第 ${d.round ?? 1} 轮）${ev.actor ? `：${actor} 点了开始` : ""}；锁定执行的数字员工，会话挂载流程空间`,
        kind: "lock",
      };
    case "instruction.sent":
      return { text: `「${node}」${INSTRUCTION[String(d.kind)] ?? "指令"}已发给数字员工`, kind: "other" };
    case "message.sent":
      return { text: `${actor} 在「${node}」给数字员工发了指令`, kind: "other" };
    case "space.uploaded":
      return { text: `${actor} 上传 ${String(d.path ?? "")}`, kind: "save" };
    case "submission.created": {
      const paths = (d.paths as string[] | undefined) ?? [];
      return { text: `${actor} 提交「${node}」第 ${d.round ?? 1} 轮：${paths.join("、")}`, kind: "review" };
    }
    case "review.opened":
      return { text: `「${node}」进入${stage}评审（${d.reviewers ?? "?"} 位评审人，${d.rule === "all" ? "全部同意" : "任一人同意"}）`, kind: "review" };
    case "vote.cast":
      return { text: `${actor} 在「${node}」${stage}评审：${d.decision === "approve" ? "同意" : "不同意"}`, kind: d.decision === "approve" ? "pass" : "reject" };
    case "review.approved":
      return { text: `「${node}」${stage}评审通过`, kind: "pass" };
    case "review.rejected":
      return {
        text: d.return_to ? `${actor} 在「${node}」准入时打回上游「${nodeName(String(d.return_to))}」` : `${actor} 驳回「${node}」，本节点再做一轮`,
        kind: "reject",
      };
    case "node.exited":
      return { text: `「${node}」已准出，等下游准入`, kind: "pass" };
    case "node.passed":
      return { text: `「${node}」已通过`, kind: "pass" };
    case "node.returned":
      return { text: `「${node}」被「${nodeName(String(d.by ?? ""))}」打回：${String(d.comment ?? "")}`, kind: "reject" };
    case "node.taken_over":
      return { text: `${actor} 接管了「${node}」：数字员工停下`, kind: "other" };
    case "node.handed_back":
      return { text: `${actor} 把「${node}」交还给数字员工${d.note ? `：${String(d.note)}` : ""}`, kind: "other" };
    case "process.completed":
      return { text: "流程完成；流程空间转只读保留", kind: "pass" };
    case "process.terminated":
      return { text: `${actor} 终止了流程${d.reason ? `：${String(d.reason)}` : ""}；流程空间转只读保留`, kind: "reject" };
    default:
      return { text: `${node ? `「${node}」` : ""}${ev.type}`, kind: "other" };
  }
}
