import type { AgentSpecIn, ModelInfo } from "@/api/types";

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
  subagents: NonNullable<AgentSpecIn["subagents"]>;
  limits: NonNullable<AgentSpecIn["limits"]>;
  compaction: NonNullable<AgentSpecIn["compaction"]>;
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
    tool_names: [],
    subagents: [],
    limits: {
      max_steps: 40,
      timeout_s: 300,
      max_total_tokens: 500_000,
      max_subagent_depth: 2,
      tool_concurrency: 4,
      require_approval_for: [],
    },
    compaction: {
      enabled: true,
      trigger_ratio: 0.75,
      target_ratio: 0.4,
      keep_recent_turns: 3,
      summarizer_model: "claude-haiku-4-5",
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
