"use client";

/**
 * 定时任务（原型 c4-model/infra-platform/ui-jobs.html）：Atlas 各服务的实例任务在回应请求的那个实例上的
 * 运行情况，按服务分页签。每个服务挂了定时任务库的只读接口（JobRoutes），面板是共享组件 @atlas/jobs-ui。
 *
 *   Atlas 服务   兜底、续租（executor/scheduling.py）              job:manage
 *   知识库       入库巡检（atlas_knowledge/jobs.py）               job:manage
 *   MCP 网关     刷新工具定义（atlas_mcpgateway/maintainer.py）    mcp:manage
 *   模型平台     LiteLLM 对账（atlas_modelgateway/reconcile.py）   model:manage
 *
 * 页签按权限显隐（只是体验，权限在各服务）。
 */
import { JobsPanel } from "@atlas/jobs-ui";
import { Tabs } from "antd";

import { useGwMe } from "@/api/mcp-gateway";
import { useMgMe } from "@/api/model-gateway";
import pageStyles from "@/features/agents/agents.module.css";
import {
  API_BASE,
  KNOWLEDGE_API_BASE,
  MCP_GATEWAY_API_BASE,
  MODEL_GATEWAY_API_BASE,
  authHeaders,
} from "@/lib/http";
import { can, useMe } from "@/lib/session";

export default function Page() {
  const { data: me } = useMe();
  const { data: gwMe } = useGwMe();
  const { data: mgMe } = useMgMe();
  const jobAdmin = can(me, "job:manage");

  const services = [
    { key: "atlas", label: "Atlas 服务", endpoint: `${API_BASE}/v1/jobs`, visible: jobAdmin },
    { key: "knowledge", label: "知识库", endpoint: `${KNOWLEDGE_API_BASE}/v1/jobs`, visible: jobAdmin },
    { key: "mcp", label: "MCP 网关", endpoint: `${MCP_GATEWAY_API_BASE}/api/v1/jobs`, visible: !!gwMe?.can_manage },
    { key: "model", label: "模型平台", endpoint: `${MODEL_GATEWAY_API_BASE}/api/v1/jobs`, visible: !!mgMe?.can_manage },
  ].filter((s) => s.visible);

  return (
    <div className={pageStyles.page}>
      <header className={pageStyles.head}>
        <div className={pageStyles.titleRow}>
          <h1 className={pageStyles.title}>定时任务</h1>
        </div>
      </header>
      <div className={pageStyles.body} style={{ paddingTop: 0 }}>
        {/* ★ 只挂当前页签的面板：不在看的服务不跟着每 5 秒刷新 */}
        <Tabs
          destroyOnHidden
          items={services.map((s) => ({
            key: s.key,
            label: s.label,
            children: <JobsPanel endpoint={s.endpoint} headers={authHeaders} />,
          }))}
        />
      </div>
    </div>
  );
}
