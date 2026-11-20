"use client";

/**
 * 新租户引导（多租户设计 §10）：新租户是空的，平台不提供任何模型、技能或智能体。租户管理员第一次
 * 进来（首页还没有能对话的智能体）时，按顺序引导：登记模型 → 创建默认智能体 → 配系统功能用的模型。
 *
 * ★ 每一步做没做完都从现有接口看出来，不另记「引导进度」：做完了自然不再出现。
 * ★ 登记模型要模型管理员权限：没有的话告诉他找谁，不给按钮。
 */
import { Button, Card, Steps } from "antd";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { useDefaultAgent } from "@/api/agents";
import { useModels } from "@/api/meta";
import { useMgMe } from "@/api/model-gateway";
import { useServiceModels } from "@/api/tenant-settings";
import { SeedModal } from "@/features/settings/DefaultAgentCard";
import { useQueryClient } from "@tanstack/react-query";

export function SetupGuide() {
  const router = useRouter();
  const qc = useQueryClient();
  const { data: mg } = useMgMe();
  const modelsQ = useModels();
  const defaultQ = useDefaultAgent();
  const serviceQ = useServiceModels();
  const [seeding, setSeeding] = useState(false);

  const hasModel = (modelsQ.data?.data ?? []).some((m) => m.selectable !== false);
  const hasDefault = !!defaultQ.data;
  const servicesReady = !!serviceQ.data && serviceQ.data.every((s) => s.ready);
  const current = !hasModel ? 0 : !hasDefault ? 1 : servicesReady ? 3 : 2;

  return (
    <Card style={{ width: "100%", maxWidth: 640 }}>
      <div style={{ fontWeight: 600, fontSize: 16 }}>开始使用</div>
      <div style={{ fontSize: 13, color: "var(--fg-3)", margin: "4px 0 16px" }}>
        这是一个新租户，还没有模型和智能体。按下面的顺序准备好，大家进来就能直接对话。
      </div>
      <Steps
        direction="vertical"
        size="small"
        current={current}
        items={[
          {
            title: "登记一个模型",
            description: hasModel ? (
              "已有可用的模型"
            ) : mg?.can_manage ? (
              <>
                <div>添加供应商账号，再登记对话模型，智能体才能回答。</div>
                <Button type="primary" size="small" style={{ marginTop: 8 }} onClick={() => router.push("/models/accounts")}>
                  去登记
                </Button>
              </>
            ) : (
              "需要模型管理员在「模型」里登记：请联系有模型管理权限的同事。"
            ),
          },
          {
            title: "创建默认智能体",
            description: hasDefault ? (
              `首页默认使用「${defaultQ.data?.name}」`
            ) : (
              <>
                <div>一键创建通用助手：共享给本租户所有人，并设为首页默认。</div>
                <Button
                  type={hasModel ? "primary" : "default"}
                  size="small"
                  style={{ marginTop: 8 }}
                  disabled={!hasModel}
                  onClick={() => setSeeding(true)}
                >
                  创建通用助手
                </Button>
              </>
            ),
          },
          {
            title: "系统功能用的模型（可选）",
            description: servicesReady ? (
              "会话标题、记忆补记、判断器都已配好"
            ) : (
              <>
                <div>会话标题、记忆补记、自动模式判断器用哪个模型；不配也能对话，这些功能会降级。</div>
                <Button size="small" style={{ marginTop: 8 }} disabled={!hasModel} onClick={() => router.push("/settings")}>
                  去设置
                </Button>
              </>
            ),
          },
        ]}
      />
      <SeedModal
        open={seeding}
        onClose={(r) => {
          setSeeding(false);
          if (r) void qc.invalidateQueries({ queryKey: ["agents"] });
        }}
      />
    </Card>
  );
}
