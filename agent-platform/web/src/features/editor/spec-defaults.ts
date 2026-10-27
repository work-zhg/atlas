import type { AgentSpecIn, ModelInfo } from "@/api/types";

/** 向用户提问的内置工具名（与后端 tool_registry.ASK_USER_TOOL 一致） */
export const ASK_USER_TOOL = "ask_user_question";

/**
 * 系统提示词里的「需求澄清」模板（设计 6.2）。通用的「何时问、怎么问」已经在工具说明里，
 * 这里只放这个领域该问哪几项 —— 作者把占位换成自己领域的清单。
 */
export const CLARIFY_TEMPLATE = `## 需求澄清
你做的东西可选方案很多。用户的需求只有一两句话时，开工前先用 ask_user_question 确认下面几项里还不清楚的
（最多 4 项，挑对结果影响最大的；用户已经说清楚的不要再问）：
- 【维度一】：选项 A / 选项 B / 选项 C
- 【维度二】：……
- 【维度三】：……
需求已经足够具体时直接做，在回复开头列出你做的假设。`;

/**
 * 编辑器内部用的 spec —— 把 tool_names / subagents / limits / compaction
 * 的可选性去掉。
 *
 * 它们在生成类型里是可选的，因为后端用了 default_factory，OpenAPI 里就不在
 * required 列表中。但表单一进来就由 defaultSpec() 填满，全程不会是 undefined，
 * 让每个读取点都写 `?.` 只是噪音。
 */
export type ResolvedSpec = AgentSpecIn & {
  tool_names: string[];
  skills: NonNullable<AgentSpecIn["skills"]>;
  subagents: NonNullable<AgentSpecIn["subagents"]>;
  limits: NonNullable<AgentSpecIn["limits"]>;
  compaction: NonNullable<AgentSpecIn["compaction"]>;
  approval: NonNullable<AgentSpecIn["approval"]>;
};

/** 与后端 schemas/agent.py 的默认值保持一致 —— 两边不一致会让新建的智能体
 *  行为与用户在表单里看到的不符。改后端默认值时这里要跟着改。 */
export function defaultSpec(model = "claude-sonnet-5"): ResolvedSpec {
  return {
    // native = 平台自己跑的图。acp（外部 CLI 跑在会话 Pod 里）目前只能经
    // API 配置，编辑器还没有这个开关。
    kind: "native",
    system_prompt: "",
    model: {
      model,
      provider: "anthropic",
      effort: null,
      thinking: "auto",
      max_output_tokens: 16_384,
      prompt_cache: true,
      temperature: null,
    },
    // 向用户提问默认开着：有界面的智能体需求不清楚时该先问（doc/ask-user-question-design.html D9）。
    // 接口创建的智能体默认不开 —— 没有人在线回答的场景不该有它
    tool_names: [ASK_USER_TOOL],
    subagents: [],
    skills: [],
    // 服务端保存时填写（记录当时的 MCP 工具定义指纹），这里给空即可
    mcp_tool_digests: {},
    mcp_drift_policy: "warn",
    limits: {
      max_steps: 40,
      timeout_s: 300,
      max_total_tokens: 500_000,
      tool_concurrency: 4,
    },
    compaction: {
      enabled: true,
      trigger_ratio: 0.75,
      target_ratio: 0.4,
      keep_recent_turns: 3,
      summarizer_model: "claude-haiku-4-5",
      clear_ratio: 0.6,
      warn_ratio: 0.9,
      keep_recent_tool_results: 5,
      restore_files: 5,
      attachment_budget_ratio: 0.1,
    },
    // 审批模式：作者定默认与允许范围，用户在会话里选（doc/native-approval-mode-design.html）
    approval: {
      default_mode: "auto",
      allowed_modes: ["plan", "manual", "auto"],
    },
  };
}

/**
 * 换模型时把不被新模型接受的参数清掉。
 *
 * 不这么做的话：从 haiku（收 temperature）切到 opus（不收）时，
 * 表单里残留的 temperature 会被一起提交，后端 spec.validate() 直接 400，
 * 而用户根本没碰过那个控件 —— 报错看上去毫无来由。
 */
export function reconcileModelSpec(
  spec: AgentSpecIn["model"],
  info: ModelInfo | undefined,
): AgentSpecIn["model"] {
  if (!info) return spec;

  const next = { ...spec };
  if (!info.supports_temperature) next.temperature = null;
  if (!info.supports_effort) next.effort = null;
  if (!info.supports_adaptive_thinking && next.thinking === "adaptive") next.thinking = "auto";
  if (next.max_output_tokens > info.max_output_tokens) {
    next.max_output_tokens = info.max_output_tokens;
  }
  return next;
}

/** effort 为 xhigh/max 时不允许关闭 thinking（engine spec.py 的约束）。 */
export function effortForbidsThinkingOff(effort: string | null | undefined): boolean {
  return effort === "xhigh" || effort === "max";
}

/**
 * 一个 agent 所挂技能描述的总长上限 —— 与后端 Settings.skill_index_budget_chars 的
 * 默认值一致（描述常驻每一轮上下文，doc/skill-mcp-backend-design.html §7.2）。
 * 超了保存会被拒；这里只是提前让人看见。
 */
export const SKILL_INDEX_BUDGET = 2000;
