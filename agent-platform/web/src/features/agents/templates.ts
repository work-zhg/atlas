/**
 * 创建智能体时的起点。模板只是一份预填好的配置：建出来就是普通智能体，之后随便改。
 *
 * ★ 「通用助手」也是平台默认智能体的推荐配置（系统 › 平台设置里一键创建）：
 *   以使用者的身份、执行命令 + 读写文件 + 计划 + 提问 + 记忆、知识库「使用者能查阅的全部」、自动执行。
 */
import type { AgentCreate } from "@/api/types";
import { ASK_USER_TOOL, defaultSpec, type ResolvedSpec } from "@/features/editor/spec-defaults";

export type TemplateId = "blank" | "general" | "coder" | "kbqa" | "data";

export interface AgentTemplate {
  id: TemplateId;
  name: string;
  description: string;
  avatarKey: string;
  spec: (model: string) => ResolvedSpec;
}

const GENERAL_PROMPT = `你是一个通用的工作助手，帮助用户完成写作、编程、数据分析、资料查找等各类任务。

工作方式：
- 复杂任务先列计划，再逐步完成
- 需要动手时直接在工作环境里执行，并说明做了什么
- 信息不足时先问清楚，不要猜
- 回答简洁，结论放在前面`;

const KBQA_PROMPT = `你根据知识库里的资料回答问题。

要求：
- 只依据检索到的资料回答，注明出处
- 资料里没有的，直接说明没有找到，不要编造
- 回答简洁，先给结论`;

const DATA_PROMPT = `你是数据分析师。用户给你表格文件或数据，你负责清洗、统计、作图并给出结论。

要求：
- 先说明你看到的数据情况（行数、字段、缺失）
- 分析过程写成脚本并实际运行，结果保存成文件
- 结论放在最前面，附上依据`;

export const TEMPLATES: AgentTemplate[] = [
  {
    id: "blank",
    name: "空白",
    description: "从零开始配置",
    avatarKey: "general",
    spec: (model) => defaultSpec(model),
  },
  {
    id: "general",
    name: "通用助手",
    description: "写作、编程、数据分析都能做",
    avatarKey: "general",
    spec: (model) => ({
      ...defaultSpec(model),
      system_prompt: GENERAL_PROMPT,
      tool_names: ["write_todos", ASK_USER_TOOL, "filesystem", "memory", "bash"],
      knowledge: { scope: "all_readable", mode: "auto", dataset_ids: [], top_k: 4, score_threshold: null, rerank: false },
    }),
  },
  {
    id: "coder",
    name: "编程助手",
    description: "用 Claude Code 在独立环境里写代码、改文件、跑测试",
    avatarKey: "coder",
    // Claude Code 驱动：镜像、适配器由服务端按部署配置补上
    spec: (model) => ({
      ...defaultSpec(model),
      kind: "cli",
      cli: { cli_type: "claude-code", adapter: "", image: "", permission_mode: "auto" },
      tool_names: [],
    }),
  },
  {
    id: "kbqa",
    name: "知识问答",
    description: "只根据指定知识库回答，附出处",
    avatarKey: "support",
    spec: (model) => ({
      ...defaultSpec(model),
      system_prompt: KBQA_PROMPT,
      tool_names: [ASK_USER_TOOL],
      knowledge: { scope: "selected", mode: "auto", dataset_ids: [], top_k: 6, score_threshold: null, rerank: false },
    }),
  },
  {
    id: "data",
    name: "数据分析",
    description: "处理表格数据，出图表和结论",
    avatarKey: "analyst",
    spec: (model) => ({
      ...defaultSpec(model),
      system_prompt: DATA_PROMPT,
      tool_names: ["write_todos", ASK_USER_TOOL, "filesystem", "bash"],
    }),
  },
];

export const templateOf = (id: TemplateId) => TEMPLATES.find((t) => t.id === id) ?? TEMPLATES[0]!;

/** 标识：名字的拼音我们不做，用模板 + 时间戳，保证不撞 */
export function newSlug(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}`;
}

export function createBody(t: AgentTemplate, name: string, model: string, description?: string): AgentCreate {
  return {
    slug: newSlug(t.id === "blank" ? "agent" : t.id),
    name,
    description: description ?? (t.id === "blank" ? "" : t.description),
    avatar_key: t.avatarKey,
    spec: t.spec(model),
  } as AgentCreate;
}
