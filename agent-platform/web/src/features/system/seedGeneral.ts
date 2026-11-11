/**
 * 一键创建平台默认的「通用助手」：用模板建一个普通智能体 → 启用 → 共享给全公司 → 设为默认 →
 * 给它的模型 Key 分配所选模型。
 *
 * ★ 每一步都是平时就有的接口，没有任何「内置智能体」特例：建出来之后和别的智能体一样可以改、
 *   可以换掉默认。
 * ★ 分配模型要模型管理员的权限：没有的话前四步照常完成，告诉用户还差这一步、找谁做。
 */
import { agentGrantsApi } from "@/api/agents";
import type { MgKey } from "@/api/model-gateway";
import type { AgentDetail } from "@/api/types";
import { createBody, templateOf } from "@/features/agents/templates";
import { http, modelGatewayHttp } from "@/lib/http";

interface DirDept {
  id: string;
  parent_id: string | null;
}

export interface SeedResult {
  agent: AgentDetail;
  /** 模型分配好了（否则要去「模型 › 客户端 Key」分配） */
  modelAssigned: boolean;
  /** 共享给了全公司（没找到根部门时为 false） */
  sharedToAll: boolean;
}

export async function seedGeneralAgent(model: string, canAssignModel: boolean): Promise<SeedResult> {
  const t = templateOf("general");
  const agent = await http.post<AgentDetail>(
    "/v1/agents",
    createBody(t, "通用助手", model, "写代码、跑命令、查资料、整理文档都能做，记得你的偏好，能查你有权限的知识库。"),
  );
  await http.post(`/v1/agents/${agent.id}/status`, { status: "enabled" });

  // 全公司 = 根部门含下级部门，可使用
  const depts = await http.get<DirDept[]>("/v1/directory/depts");
  const root = depts.find((d) => !d.parent_id);
  if (root) {
    await agentGrantsApi.add(agent.id, {
      level: "READ",
      subjects: [{ type: "dept", id: root.id }],
      include_sub: true,
    });
  }
  await http.put("/v1/agents/default", { agent_id: agent.id });

  let modelAssigned = false;
  if (canAssignModel) {
    // 智能体的客户端 Key 随创建建好（待分配）：类型 Agent、编码 = 智能体 id
    const keys = await modelGatewayHttp.get<MgKey[]>("/api/v1/client-keys");
    const key = keys.find((k) => k.client_type === "Agent" && k.client_code === agent.id);
    if (key) {
      await modelGatewayHttp.put(`/api/v1/client-keys/${key.uuid}/models`, { models: [model] });
      modelAssigned = true;
    }
  }
  return { agent, modelAssigned, sharedToAll: Boolean(root) };
}
