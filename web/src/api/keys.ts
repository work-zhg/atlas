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

  runs: {
    all: ["runs"] as const,
    detail: (id: string) => ["runs", "detail", id] as const,
  },
} as const;
