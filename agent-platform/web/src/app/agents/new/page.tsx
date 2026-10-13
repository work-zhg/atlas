"use client";

import { AgentEditor } from "@/features/editor/AgentEditor";

/** 静态路由优先于 [agentId]，所以 /agents/new 不会被当成 id 为 "new" 的智能体。 */
export default function NewAgentPage() {
  return <AgentEditor />;
}
