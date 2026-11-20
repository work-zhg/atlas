"use client";

/**
 * 租户设置（多租户设计 §3.3）：租户管理员管自己租户的地方 —— 份额与用量、默认智能体、租户指令、
 * 系统功能用的模型。每人上限等租户设置项在「配置项」里改（有修改历史、能回滚）。
 *
 * ★ 份额、镜像、定时任务这类平台的事在运营中心，不在这里。
 */
import { Card } from "antd";

import { SETTINGS_MANAGE } from "@/api/tenant-settings";
import pageStyles from "@/features/agents/agents.module.css";
import { InstructionEditor } from "@/features/me/InstructionEditor";
import { CapacityCard } from "@/features/settings/CapacityCard";
import { DefaultAgentCard } from "@/features/settings/DefaultAgentCard";
import { ServiceModelsCard } from "@/features/settings/ServiceModelsCard";
import { can, useMe } from "@/lib/session";

export default function Page() {
  const { data: me } = useMe();
  const canManage = can(me, SETTINGS_MANAGE);
  return (
    <div className={pageStyles.page}>
      <header className={pageStyles.head}>
        <div className={pageStyles.titleRow}>
          <h1 className={pageStyles.title}>租户设置</h1>
        </div>
      </header>
      <div className={pageStyles.body} style={{ maxWidth: 900, display: "flex", flexDirection: "column", gap: 16 }}>
        <CapacityCard />
        <DefaultAgentCard canManage={canManage} />
        <Card>
          <div style={{ fontWeight: 600 }}>组织指令</div>
          <div style={{ fontSize: 13, color: "var(--fg-3)", margin: "2px 0 12px" }}>
            对本租户所有人的所有对话生效，适合写合规要求和公司约定
          </div>
          <InstructionEditor
            scope="tenant"
            placeholder="例如：不要输出客户的手机号和身份证号；涉及金额的回答需注明数据来源。"
            hint="下一轮对话生效"
          />
        </Card>
        <ServiceModelsCard canManage={canManage} />
      </div>
    </div>
  );
}
