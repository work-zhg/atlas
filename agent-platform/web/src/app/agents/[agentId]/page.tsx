"use client";

import { useParams } from "next/navigation";

import { AgentStudio } from "@/features/editor/AgentStudio";

/** 编辑智能体：左边配置、右边调试预览 */
export default function AgentEditorPage() {
  const { agentId } = useParams<{ agentId: string }>();
  return <AgentStudio agentId={agentId} />;
}
