/**
 * 工具调用 → 给人看的「步骤」：阅读 report.md、运行命令 pnpm test、查看记忆：工作。
 *
 * ★ 只认平台自己的工具名（kernel / server 注册的那几个）。CLI 子智能体的工具名本来就是
 *   CLI 给的标题（「Write hello.py」），MCP 工具是 服务名__工具名 —— 这两类原样显示，
 *   不去猜它们是什么动作。
 */
import type { IconName } from "../components/Icon";
import type { ToolCall } from "./run-reducer";

export interface StepView {
  icon: IconName;
  /** 动作：阅读、运行命令…… */
  label: string;
  /** 动作的对象：文件、命令、关键词 */
  target: string;
}

/** 工具名 → [图标, 动作, 从参数里取对象的字段（按顺序取第一个有值的）] */
const KNOWN: Record<string, [IconName, string, string[]]> = {
  read_file: ["file", "阅读", ["file_path", "path"]],
  write_file: ["pencil", "写入", ["file_path", "path"]],
  edit_file: ["pencil", "修改", ["file_path", "path"]],
  ls: ["folder", "查看目录", ["path"]],
  glob: ["search", "查找文件", ["pattern"]],
  grep: ["search", "搜索", ["pattern"]],
  execute: ["terminal", "运行命令", ["command"]],
  write_todos: ["todo", "更新计划", []],
  load_memory: ["bookmark", "查看记忆", ["topic"]],
  save_memory: ["bookmark", "记住", ["content"]],
  forget_memory: ["bookmark", "忘记", []],
  knowledge_search: ["book", "查找知识库", ["query"]],
  tool_search: ["search", "查找工具", ["query"]],
  ask_user_question: ["help", "向你提问", []],
};

const MCP_SEP = "__";

function pick(args: unknown, fields: string[]): string {
  if (!args || typeof args !== "object") return "";
  const obj = args as Record<string, unknown>;
  for (const f of fields) {
    const v = obj[f];
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  return "";
}

/** 一行放得下的对象：多行命令只取第一行 */
function oneLine(s: string, max = 80): string {
  const first = s.split("\n")[0] ?? "";
  return first.length > max ? `${first.slice(0, max)}…` : first;
}

export function stepView(call: ToolCall): StepView {
  const known = KNOWN[call.name];
  if (known) {
    const [icon, label, fields] = known;
    return { icon, label, target: oneLine(pick(call.args, fields) || (fields.length ? call.argsPreview ?? "" : "")) };
  }
  if (call.name.includes(MCP_SEP)) {
    // MCP：服务名__工具名
    const [server, ...rest] = call.name.split(MCP_SEP);
    return { icon: "plug", label: `调用 ${server} · ${rest.join(MCP_SEP)}`, target: oneLine(call.argsPreview ?? "") };
  }
  // CLI 的工具：名字就是它给的标题
  return { icon: "wrench", label: call.name, target: oneLine(call.argsPreview ?? "") };
}

/** 工具名给人看的叫法（审批卡片里用）：平台自己的工具说动作，其余原样 */
export function toolLabel(name: string): string {
  return KNOWN[name]?.[1] ?? name;
}

/**
 * 一组步骤的标题：进行中说正在做什么，结束了说做了几步。
 * live = 这一轮还在跑；历史轮次里仍标着 running 的是没等到结果就结束了的，不说「正在」。
 */
export function stepsTitle(calls: ToolCall[], live = true): string {
  const running = live ? [...calls].reverse().find((c) => c.status === "running") : undefined;
  if (running) {
    const v = stepView(running);
    return `正在${v.label}${v.target ? ` ${v.target}` : ""}…`;
  }
  const failed = calls.filter((c) => c.status === "error").length;
  return failed
    ? `已完成 ${calls.length} 个步骤，${failed} 个没成功`
    : `已完成 ${calls.length} 个步骤`;
}

export function stepDuration(ms: number | undefined): string {
  if (ms === undefined) return "";
  return ms < 1000 ? `${Math.max(ms, 1)}ms` : `${(ms / 1000).toFixed(1)}s`;
}
