/**
 * Query key 工厂 —— 所有 key 从这里出，避免各处手拼字符串导致 invalidate 打空。
 *
 * 层级设计成前缀可失效：invalidate(qk.threads.all) 会连带失效列表和详情。
 */
export const qk = {
  models: ["models"] as const,
  tools: ["tools"] as const,

  agents: {
    all: ["agents"] as const,
    list: (status?: string) => ["agents", "list", status ?? "all"] as const,
    detail: (id: string) => ["agents", "detail", id] as const,
    versions: (id: string) => ["agents", "versions", id] as const,
  },

  threads: {
    all: ["threads"] as const,
    list: (agentId?: string) => ["threads", "list", agentId ?? "all"] as const,
    detail: (id: string) => ["threads", "detail", id] as const,
    messages: (id: string) => ["threads", "messages", id] as const,
  },

  /** 技能 / MCP。运行时视图（引用、用量、MCP 当前定义）与配置服务视图分开缓存 */
  catalog: {
    skills: ["catalog", "skills"] as const,
    skillRefs: (slug: string) => ["catalog", "skills", "refs", slug] as const,
    skillUsage: (days: number) => ["catalog", "skills", "usage", days] as const,
    mcp: ["catalog", "mcp"] as const,
    mcpServer: (name: string) => ["catalog", "mcp", name] as const,
  },
  config: {
    skills: ["config", "skills"] as const,
    skill: (slug: string) => ["config", "skills", slug] as const,
    file: (key: string) => ["config", "file", key] as const,
    mcp: ["config", "mcp"] as const,
    reviews: (name: string) => ["config", "mcp", "reviews", name] as const,
  },

  runs: {
    all: ["runs"] as const,
    detail: (id: string) => ["runs", "detail", id] as const,
    trace: (id: string) => ["runs", "trace", id] as const,
    /** 按审批 id 分键：新审批到来时必须重新查库，不能拿同一 run 上次的缓存结论。 */
    approvals: (runId: string, approvalId: string) =>
      ["runs", "approvals", runId, approvalId] as const,
  },
} as const;
