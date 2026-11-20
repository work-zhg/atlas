"use client";

/**
 * 系统功能用的模型（模型管理方案设计第 9 章）：会话标题、记忆补记、自动模式判断器这类不属于某个
 * 智能体的调用，每个租户各配各的。
 *
 * ★ 每种用途一把「平台服务」客户端 Key（类型 Service、编码见 SERVICE_CODES）：这里选好模型，
 *   没有 Key 就在模型平台上建、有了就把模型加进去，再把用途指过去 —— 和部署脚本建的是同一把。
 *   建 Key、分配模型要模型管理员的权限；没有的话只能看。
 * ★ 没配的功能会降级（标题用问题截断、补记下次再做、判断器转人工），不影响对话。
 */
import { App as AntdApp, Button, Card, Modal, Popconfirm, Select, Skeleton, Tag, Tooltip } from "antd";
import { useState } from "react";

import { useMgKeyAdminActions, useMgKeys, useMgMe, useMgModels } from "@/api/model-gateway";
import { type ServiceModel, useServiceModelActions, useServiceModels } from "@/api/tenant-settings";

/** 用途 → 平台服务客户端 Key 的编码（与 deploy/server/model-cutover.sh 建的一致） */
const SERVICE_CODES: Record<string, string> = {
  title: "session-title",
  memory_backfill: "memory-backfill",
  approval_judge: "approval-judge",
};
const SERVICE_TYPE = "Service";

const HINTS: Record<string, string> = {
  title: "没配：标题用第一句话截断",
  memory_backfill: "没配：对话里没记下的偏好不会补记",
  approval_judge: "没配：自动模式下要判断的操作一律转人工确认",
};

function Status({ row }: { row: ServiceModel }) {
  if (!row.ready) return <Tag color="orange">{row.model ? "用不了" : "未配置"}</Tag>;
  return row.model ? <Tag color="green">可用</Tag> : <Tag>用部署配置里的模型</Tag>;
}

function EditModal({ row, onClose }: { row: ServiceModel; onClose: () => void }) {
  const { message } = AntdApp.useApp();
  const modelsQ = useMgModels();
  const code = SERVICE_CODES[row.purpose] ?? row.purpose;
  const keysQ = useMgKeys({ key_type: "client", client_type: SERVICE_TYPE, q: code });
  const keyActions = useMgKeyAdminActions();
  const { set } = useServiceModelActions();
  const [model, setModel] = useState<string | undefined>(row.model ?? undefined);
  const [busy, setBusy] = useState(false);
  const models = (modelsQ.data ?? []).filter((m) => m.kind === "chat" && m.enabled);

  const save = async () => {
    if (!model) return;
    setBusy(true);
    try {
      const key = (keysQ.data ?? []).find((k) => k.client_code === code);
      if (!key) {
        await keyActions.createClient.mutateAsync({
          client_type: SERVICE_TYPE,
          client_code: code,
          name: row.name,
          label: "",
          models: [model],
          monthly_budget: null,
          rpm: null,
          expires_at: null,
        });
      } else if (!key.models.some((m) => m.name === model)) {
        await keyActions.update.mutateAsync({ id: key.id, body: { models: [...key.models.map((m) => m.name), model] } });
      }
      await set.mutateAsync({ purpose: row.purpose, client_type: SERVICE_TYPE, client_code: code, model });
      message.success(`「${row.name}」改用 ${model}`);
      onClose();
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      title={`${row.name}用哪个模型`}
      okText="保存"
      cancelText="取消"
      confirmLoading={busy}
      okButtonProps={{ disabled: !model || keysQ.isPending }}
      onCancel={onClose}
      onOk={() => void save()}
    >
      <p style={{ color: "var(--fg-2)", marginTop: 0 }}>
        用模型平台上的客户端 Key「{SERVICE_TYPE}:{code}」调用：没有会自动建好，并把选的模型分配给它。
      </p>
      <Select
        style={{ width: "100%" }}
        placeholder="选择模型"
        loading={modelsQ.isPending}
        value={model}
        onChange={setModel}
        options={models.map((m) => ({ value: m.name, label: m.display_name || m.name }))}
        notFoundContent="还没有可用的对话模型：先在「模型」里登记一个"
      />
    </Modal>
  );
}

export function ServiceModelsCard({ canManage }: { canManage: boolean }) {
  const { message } = AntdApp.useApp();
  const { data: mg } = useMgMe();
  const q = useServiceModels(canManage);
  const { clear } = useServiceModelActions();
  const [editing, setEditing] = useState<ServiceModel>();
  const canAssign = !!mg?.can_manage;

  return (
    <Card>
      <div style={{ fontWeight: 600 }}>系统功能用的模型</div>
      <div style={{ fontSize: 13, color: "var(--fg-3)", margin: "2px 0 8px" }}>
        会话标题、记忆补记、自动模式判断器不属于某个智能体，用这里配的模型；没配的功能会降级，不影响对话
      </div>
      {!canManage ? (
        <div style={{ color: "var(--fg-3)" }}>需要租户管理员权限</div>
      ) : q.isPending ? (
        <Skeleton active paragraph={{ rows: 3 }} />
      ) : (
        (q.data ?? []).map((row) => (
          <div key={row.purpose} style={{ display: "flex", alignItems: "center", gap: 12, padding: "12px 0", borderTop: "1px solid var(--line)" }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span style={{ fontWeight: 600 }}>{row.name}</span>
                <Status row={row} />
              </div>
              <div style={{ fontSize: 12.5, color: "var(--fg-3)", marginTop: 2 }}>
                {row.model ? (
                  <>
                    {row.model}
                    <span style={{ fontFamily: "var(--mono)", marginLeft: 8 }}>
                      {row.client_type}:{row.client_code}
                    </span>
                  </>
                ) : (
                  HINTS[row.purpose]
                )}
              </div>
              {row.problem && row.model && <div style={{ fontSize: 12.5, color: "var(--warning)", marginTop: 2 }}>{row.problem}</div>}
            </div>
            <Tooltip title={canAssign ? undefined : "要在模型平台上建 Key、分配模型，需要模型管理员权限"}>
              <Button size="small" disabled={!canAssign} onClick={() => setEditing(row)}>
                {row.model ? "更换" : "设置"}
              </Button>
            </Tooltip>
            {row.model && (
              <Popconfirm
                title={`清除后「${row.name}」会降级`}
                okText="清除"
                cancelText="取消"
                onConfirm={() =>
                  clear.mutateAsync(row.purpose).then(
                    () => message.success("已清除"),
                    (e: Error) => message.error(e.message),
                  )
                }
              >
                <Button size="small" type="text">
                  清除
                </Button>
              </Popconfirm>
            )}
          </div>
        ))
      )}
      {editing && <EditModal row={editing} onClose={() => setEditing(undefined)} />}
    </Card>
  );
}
