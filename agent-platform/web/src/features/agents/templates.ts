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
- 每个问题都先用 knowledge_search 查知识库，再依据查到的资料回答，注明出处
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
      // 使用者能读的全部知识集：由模型看目录决定查不查（知识集方案 §8）
      knowledge: { scope: "all_readable", set_ids: [] },
    }),
  },
  {
    id: "coder",
    name: "编程助手",
    description: "用 Claude Code 在独立环境里写代码、改文件、跑测试",
    avatarKey: "coder",
    // Claude Code 驱动：镜像、适配器由服务端按部署配置补上。跑在哪里创建时再选（placeOn）
    spec: (model) => ({
      ...defaultSpec(model),
      kind: "cli",
      cli: {
        cli_type: "claude-code",
        adapter: "",
        image: "",
        permission_mode: "auto",
        placement: "pod",
        default_project_dir: "",
        model_source: "platform",
      },
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
      // 知识集由建设者自己添加（选库、写介绍）；平台不自动检索，提示词要求每个问题先查
      knowledge: { scope: "selected", set_ids: [] },
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

/** CLI 智能体跑在哪：平台的会话 Pod，或使用者自己的电脑。建好后不能改 */
export type CliWhere = "pod" | "device";

/** 本机 CLI 的默认项目地址：新对话、被委派时的目录都是「它 / 对话 id」 */
export const DEFAULT_PROJECT_DIR = "~/atlas-projects";

const LOCAL_CODER_PROMPT = `你在使用者自己的电脑上工作，项目目录里的代码就是他正在开发的项目。
改动前先说明要改什么；跑测试时优先用项目自带的脚本。`;

/** 编程助手放到使用者自己的电脑上（本机 CLI 方案 §2.1）：没有镜像，用电脑上装好的 CLI */
export function placeOn(spec: ResolvedSpec, where: CliWhere): ResolvedSpec {
  if (where === "pod" || !spec.cli) return spec;
  return {
    ...spec,
    system_prompt: spec.system_prompt || LOCAL_CODER_PROMPT,
    cli: { ...spec.cli, placement: "device", image: "", adapter: "", default_project_dir: DEFAULT_PROJECT_DIR },
  };
}

export function createBody(
  t: AgentTemplate,
  name: string,
  model: string,
  description?: string,
  where: CliWhere = "pod",
): AgentCreate {
  const onDevice = where === "device" && t.id === "coder";
  return {
    slug: newSlug(t.id === "blank" ? "agent" : onDevice ? "local-coder" : t.id),
    name,
    description: onDevice ? "用你电脑上的 Claude Code 改你电脑上的项目" : (description ?? (t.id === "blank" ? "" : t.description)),
    avatar_key: t.avatarKey,
    spec: onDevice ? placeOn(t.spec(model), where) : t.spec(model),
  } as AgentCreate;
}
