"use client";

/** 默认智能体：首页输入框默认用它（租户设置 tenant.default_agent，改要 settings:manage）。 */
import { App as AntdApp, Alert, Button, Card, Modal, Select, Space } from "antd";
import Link from "next/link";
import { useState } from "react";

import { useAgents, useDefaultAgent, useSetDefaultAgent } from "@/api/agents";
import { useModels } from "@/api/meta";
import { useMgMe } from "@/api/model-gateway";
import { avatarBackground, avatarLetter } from "@/features/agents/avatar";
import { seedGeneralAgent, type SeedResult } from "@/features/settings/seedGeneral";
import { useQueryClient } from "@tanstack/react-query";

/** 一键创建通用助手（租户设置、新租户引导共用） */
export function SeedModal({ open, onClose }: { open: boolean; onClose: (r?: SeedResult) => void }) {
  const { message } = AntdApp.useApp();
  const modelsQ = useModels();
  const { data: mg } = useMgMe();
  const models = (modelsQ.data?.data ?? []).filter((m) => m.selectable !== false);
  const [model, setModel] = useState<string>();
  const [busy, setBusy] = useState(false);
  const picked = model ?? models[0]?.model;
  return (
    <Modal
      open={open}
      title="创建通用助手"
      okText="创建并设为默认"
      cancelText="取消"
      confirmLoading={busy}
      okButtonProps={{ disabled: !picked }}
      onCancel={() => onClose()}
      onOk={async () => {
        if (!picked) return;
        setBusy(true);
        try {
          onClose(await seedGeneralAgent(picked, !!mg?.can_manage));
        } catch (e) {
          message.error((e as Error).message);
        } finally {
          setBusy(false);
        }
      }}
    >
      <p style={{ color: "var(--fg-2)", marginTop: 0 }}>
        能执行命令、读写文件、制定计划、记住偏好，能查使用者有权限的全部知识库；共享给本租户所有人，进来就能直接用。之后可以在「智能体」里随时修改。
      </p>
      <div style={{ fontWeight: 600, margin: "12px 0 6px" }}>模型</div>
      <Select
        style={{ width: "100%" }}
        loading={modelsQ.isPending}
        value={picked}
        onChange={setModel}
        options={models.map((m) => ({ value: m.model, label: m.display_name || m.model }))}
      />
    </Modal>
  );
}

export function DefaultAgentCard({ canManage }: { canManage: boolean }) {
  const { message } = AntdApp.useApp();
  const qc = useQueryClient();
  const defaultQ = useDefaultAgent();
  const agentsQ = useAgents({ status: "enabled" });
  const setDefault = useSetDefaultAgent();
  const [seeding, setSeeding] = useState(false);
  const [result, setResult] = useState<SeedResult>();
  const current = defaultQ.data;

  return (
    <Card>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 16 }}>
        <div style={{ flex: 1 }}>
          <div style={{ fontWeight: 600 }}>默认智能体</div>
          <div style={{ fontSize: 13, color: "var(--fg-3)", marginTop: 2 }}>首页输入框默认使用它；用户可以在输入框里换成别的</div>
        </div>
        <Space>
          <Select
            style={{ width: 240 }}
            placeholder="选择智能体"
            loading={agentsQ.isPending || defaultQ.isPending}
            value={current?.id}
            disabled={!canManage}
            allowClear
            onChange={(id?: string) =>
              setDefault.mutate(id ?? null, {
                onSuccess: () => message.success(id ? "已设为默认智能体" : "已取消默认智能体"),
                onError: (e) => message.error(e.message),
              })
            }
            options={(agentsQ.data?.data ?? []).map((a) => ({
              value: a.id,
              label: (
                <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                  <span
                    style={{ width: 18, height: 18, borderRadius: 5, background: avatarBackground(a.avatar_key), color: "#fff", fontSize: 10, fontWeight: 700, display: "inline-grid", placeItems: "center" }}
                  >
                    {avatarLetter(a.name)}
                  </span>
                  {a.name}
                </span>
              ),
            }))}
          />
        </Space>
      </div>
      {canManage && !defaultQ.isPending && !current && (
        <Alert
          style={{ marginTop: 16 }}
          type="info"
          showIcon
          message="还没有默认智能体"
          description="可以一键创建一个通用助手：用推荐配置建好、共享给本租户所有人并设为默认。"
          action={
            <Button type="primary" onClick={() => setSeeding(true)}>
              创建通用助手
            </Button>
          }
        />
      )}
      {result && (
        <Alert
          style={{ marginTop: 16 }}
          type={result.modelAssigned ? "success" : "warning"}
          showIcon
          message={`已创建「${result.agent.name}」并设为默认`}
          description={
            <>
              {!result.sharedToAll && <div>没找到公司根部门，还没共享给其他人：请在智能体的「共享」里添加。</div>}
              {result.modelAssigned ? (
                <div>
                  已分配模型，现在就能用了。<Link href={`/agents/${result.agent.id}`}>查看配置</Link>
                </div>
              ) : (
                <div>
                  还需要模型管理员在「模型 › Key 管理」里给它分配模型，分配之前它还不能回答。
                </div>
              )}
            </>
          }
        />
      )}
      <SeedModal
        open={seeding}
        onClose={(r) => {
          setSeeding(false);
          if (r) {
            setResult(r);
            void qc.invalidateQueries({ queryKey: ["agents"] });
          }
        }}
      />
    </Card>
  );
}
