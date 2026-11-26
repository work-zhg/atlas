"use client";

/**
 * 对一个租户做的事：编辑（名称、名额、到期）、补发首个管理员的临时密码、停用、恢复、关闭。
 * 列表的「⋯」和详情页共用。
 *
 * ★ 停用只挡用户中心的入口（业务系统不知道租户状态），数据都留着；关闭只能对已停用的做，要输入企业代码确认，不可恢复。
 * ★ 有操作在进行时不能再发起停用、恢复、关闭（一次只做一件）。
 * ★ 都属于租户模块：编辑、停用、恢复要读写；关闭（删数据）、补发临时密码要 Owner。
 *   系统租户（运营人员靠它登录运营中心）不能停用、关闭。
 */
import { useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Form, Input, InputNumber, Modal, type MenuProps } from "antd";
import { useState } from "react";

import { ErrorBox, OBSERVER_TIP, OWNER_TIP, OnceBox, errorText } from "@/components/common";
import { Icon } from "@/components/Icon";
import { api } from "@/lib/api";
import type { Tenant } from "@/lib/types";

export function useTenantActions(admin: boolean, owner: boolean) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [edit, setEdit] = useState<Tenant | null>(null);
  const [suspending, setSuspending] = useState<Tenant | null>(null);
  const [closing, setClosing] = useState<Tenant | null>(null);
  const [secret, setSecret] = useState<{ tenant: Tenant; temp: string } | null>(null);

  const refresh = (t: Tenant) =>
    Promise.all([qc.invalidateQueries({ queryKey: ["tenants"] }), qc.invalidateQueries({ queryKey: ["tenant", t.id] })]);

  async function run(t: Tenant, fn: () => Promise<unknown>, ok: string) {
    try {
      await fn();
      message.success(ok);
      await refresh(t);
      return true;
    } catch (e) {
      message.error(errorText(e));
      return false;
    }
  }

  async function reissue(t: Tenant) {
    try {
      const out = await api.post<{ temp_password: string }>(`/tenants/${t.id}/admin-password`);
      setSecret({ tenant: t, temp: out.temp_password });
    } catch (e) {
      message.error(errorText(e));
    }
  }

  const resume = (t: Tenant) => run(t, () => api.post(`/tenants/${t.id}/resume`), "已发起恢复：放开用户中心的入口");

  /** 列表行尾、详情页「⋯」里的菜单 */
  function menu(t: Tenant, opts: { withView?: () => void } = {}): MenuProps {
    const busy = !!t.operation;
    const dis = !admin;
    // 系统租户不能停用、关闭：运营人员都靠它登录运营中心
    const sys = !!t.is_system;
    const items: MenuProps["items"] = [
      ...(opts.withView ? [{ key: "view", icon: <Icon name="eye" size={14} />, label: "查看" }] : []),
      ...(t.status !== "closed"
        ? [
            { key: "edit", icon: <Icon name="pencil" size={14} />, label: "编辑名称、名额、到期", disabled: dis },
            ...(t.status === "active"
              ? [{ key: "reissue", icon: <Icon name="key" size={14} />, label: "补发首个管理员的临时密码", disabled: !owner }]
              : []),
            { type: "divider" as const },
            t.status === "active"
              ? { key: "suspend", icon: <Icon name="pause" size={14} />, label: sys ? "停用（系统租户不能停用）" : "停用", disabled: dis || busy || sys }
              : { key: "resume", icon: <Icon name="play" size={14} />, label: "恢复", disabled: dis || busy },
            ...(t.status === "suspended"
              ? [{ key: "close", icon: <Icon name="trash" size={14} />, label: "关闭并删除数据", danger: true, disabled: !owner || busy || sys }]
              : []),
          ]
        : []),
    ];
    return {
      items,
      onClick: ({ key, domEvent }) => {
        domEvent.stopPropagation();
        if (key === "view") opts.withView?.();
        if (key === "edit") setEdit(t);
        if (key === "reissue") void reissue(t);
        if (key === "suspend") setSuspending(t);
        if (key === "resume") void resume(t);
        if (key === "close") setClosing(t);
      },
    };
  }

  const element = (
    <>
      {edit && <EditTenant tenant={edit} onClose={() => setEdit(null)} onSaved={() => refresh(edit)} />}
      {suspending && (
        <SuspendTenant
          tenant={suspending}
          onClose={() => setSuspending(null)}
          onOk={(reason) => run(suspending, () => api.post(`/tenants/${suspending.id}/suspend`, { reason: reason || null }), "已发起停用")}
        />
      )}
      {closing && (
        <CloseTenant
          tenant={closing}
          onClose={() => setClosing(null)}
          onOk={(code) => run(closing, () => api.post(`/tenants/${closing.id}/close`, { confirm_code: code }), "已发起关闭")}
        />
      )}
      {secret && (
        <Modal
          open
          title="补发临时密码"
          maskClosable={false}
          onCancel={() => setSecret(null)}
          footer={
            <Button type="primary" onClick={() => setSecret(null)}>
              我已交给企业管理员
            </Button>
          }
        >
          <OnceBox
            rows={[
              ["企业代码", secret.tenant.code],
              ["临时密码", secret.temp],
            ]}
            warn="旧的临时密码已作废。新的只显示这一次；首个管理员还没登录过时才能补发"
          />
        </Modal>
      )}
    </>
  );

  return {
    menu,
    resume,
    reissue,
    edit: setEdit,
    suspend: setSuspending,
    close: setClosing,
    element,
    admin,
    owner,
    tip: admin ? undefined : OBSERVER_TIP,
    ownerTip: owner ? undefined : OWNER_TIP,
  };
}

function SuspendTenant({ tenant, onClose, onOk }: { tenant: Tenant; onClose: () => void; onOk: (reason: string) => Promise<boolean> }) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <Modal
      open
      title={`停用 ${tenant.name}`}
      okText="停用"
      okButtonProps={{ danger: true }}
      confirmLoading={busy}
      onCancel={onClose}
      onOk={async () => {
        setBusy(true);
        if (await onOk(reason)) onClose();
        setBusy(false);
      }}
    >
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 14 }}
        message="挡住用户中心的入口：这个企业的人登录不了，各系统按权限一律拒绝。已经在跑的对话、入库会跑完；数据都留着，恢复后接着用"
      />
      <div className="ops-sub" style={{ marginBottom: 6 }}>
        原因（会记进运营审计和操作记录）
      </div>
      <Input.TextArea rows={2} placeholder="如：合同到期未续" value={reason} onChange={(e) => setReason(e.target.value)} />
    </Modal>
  );
}

function CloseTenant({ tenant, onClose, onOk }: { tenant: Tenant; onClose: () => void; onOk: (code: string) => Promise<boolean> }) {
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <Modal
      open
      title={`关闭 ${tenant.name}`}
      okText="关闭并删除数据"
      okButtonProps={{ danger: true, disabled: code !== tenant.code }}
      confirmLoading={busy}
      onCancel={onClose}
      onOk={async () => {
        setBusy(true);
        if (await onOk(code)) onClose();
        setBusy(false);
      }}
    >
      <Alert
        type="error"
        showIcon
        style={{ marginBottom: 14 }}
        message="各系统会删除这个企业的全部数据（会话、文件、知识库、技能、模型、MCP、记忆……），最后删除用户中心里的租户。删了找不回来，企业代码也不能再用"
      />
      <div className="ops-sub" style={{ marginBottom: 6 }}>
        输入企业代码 <b className="ops-mono">{tenant.code}</b> 确认
      </div>
      <Input placeholder={tenant.code} value={code} onChange={(e) => setCode(e.target.value.trim())} />
    </Modal>
  );
}

function EditTenant({ tenant, onClose, onSaved }: { tenant: Tenant; onClose: () => void; onSaved: () => Promise<unknown> }) {
  const [form] = Form.useForm();
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(v: { name: string; max_users?: number | null; expires_on?: string }) {
    setBusy(true);
    try {
      await api.patch(`/tenants/${tenant.id}`, {
        name: v.name,
        max_users: v.max_users || null,
        expires_at: v.expires_on ? new Date(`${v.expires_on}T23:59:59`).toISOString() : null,
      });
      await onSaved();
      onClose();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open title={`编辑 ${tenant.name}`} onCancel={onClose} onOk={() => form.submit()} okText="保存" confirmLoading={busy}>
      <ErrorBox error={error} />
      <Form
        form={form}
        layout="vertical"
        requiredMark={false}
        initialValues={{
          name: tenant.name,
          max_users: tenant.max_users,
          expires_on: tenant.expires_at ? tenant.expires_at.slice(0, 10) : undefined,
        }}
        onFinish={submit}
      >
        <Form.Item name="name" label="名称" rules={[{ required: true, message: "请输入名称" }]}>
          <Input />
        </Form.Item>
        <div className="ops-cols-2">
          <Form.Item name="max_users" label="人员名额" extra="不填 = 不限；少于现有人数时不能再加人">
            <InputNumber min={1} style={{ width: "100%" }} placeholder="不限" />
          </Form.Item>
          <Form.Item name="expires_on" label="到期日" extra="不填 = 不到期；到期后这个企业的人登录不了、没有权限，续期后立即恢复">
            <Input type="date" />
          </Form.Item>
        </div>
      </Form>
    </Modal>
  );
}
