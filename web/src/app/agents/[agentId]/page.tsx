"use client";

import { useParams } from "next/navigation";

import { AgentEditor } from "@/features/editor/AgentEditor";

export default function AgentEditorPage() {
  const { agentId } = useParams<{ agentId: string }>();
  return <AgentEditor agentId={agentId} />;
}
