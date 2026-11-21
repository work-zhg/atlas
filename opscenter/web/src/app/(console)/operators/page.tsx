"use client";

/**
 * 运营账号：不在用户中心的任何租户里。只有两个角色：运营管理员（全部运营操作）、运营观察员（只读）。
 * 新账号、重置密码给一次性临时密码，首次登录必须改。至少保留一个可用的运营管理员；自己的账号不能停用、重置。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Dropdown, Form, Input, Modal, Table, Tag, Tooltip } from "antd";
import { useState } from "react";

import { ErrorBox, OBSERVER_TIP, OnceBox, PageHead, PersonAvatar, errorText } from "@/components/common";
import { Icon } from "@/components/Icon";
import { BASE_PATH, api } from "@/lib/api";
import { dateTime } from "@/lib/format";
import { useMe } from "@/lib/session";
import type { Operator, Role } from "@/lib/types";

const ROLES: { value: Role; label: string; desc: string }[] = [
  { value: "ADMIN", label: "运营管理员", desc: "开通、停用、关闭企业，改份额和系统配置，管运营账号" },
  { value: "OBSERVER", label: "运营观察员", desc: "什么都能看，什么都不能改；适合审计、值班查看" },
];
const roleLabel = (r: Role) => ROLES.find((x) => x.value === r)?.label ?? r;

export default function OperatorsPage() {
  const { data: me } = useMe();
  const admin = me?.role === "ADMIN";
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const q = useQuery({ queryKey: ["operators"], queryFn: () => api.get<Operator[]>("/operators") });
  const [creating, setCreating] = useState(false);
  const [secret, setSecret] = useState<{ title: string; account: string; value: string; warn: string } | null>(null);

  async function act(fn: () => Promise<unknown>, ok: string) {
    try {
      await fn();
      message.success(ok);
      await qc.invalidateQueries({ queryKey: ["operators"] });
    } catch (e) {
      message.error(errorText(e));
    }
  }

  function reset(o: Operator) {
    modal.confirm({
      title: `重置 ${o.account} 的密码`,
      content: "他的全部登录作废、锁定解除，给一个新的临时密码。",
      okText: "重置",
      onOk: async () => {
        try {
          const out = await api.post<{ temp_password: string }>(`/operators/${o.id}/reset-password`);
          setSecret({ title: "密码已重置", account: o.account, value: out.temp_password, warn: "旧密码已失效、锁定已解除。临时密码只显示这一次，他登录后要改成自己的" });
          await qc.invalidateQueries({ queryKey: ["operators"] });
        } catch (e) {
          message.error(errorText(e));
        }
      },
    });
  }

  return (
    <>
      <PageHead
        title="运营账号"
        sub="运营中心自己的账号，和各企业的账号分开：一个人同时是某个企业的员工也没关系，两个账号、分开登录"
        extra={
          <Tooltip title={admin ? undefined : OBSERVER_TIP}>
            <Button type="primary" icon={<Icon name="userPlus" />} disabled={!admin} onClick={() => setCreating(true)}>
              开账号
            </Button>
          </Tooltip>
        }
      />
      <ErrorBox error={q.error} />
      <Table<Operator>
        rowKey="id"
        loading={q.isLoading}
        dataSource={q.data ?? []}
        pagination={false}
        columns={[
          {
            title: "账号",
            render: (_, o) => (
              <div className="ops-row" style={{ gap: 10 }}>
                <PersonAvatar name={o.name} />
                <div>
                  <div className="ops-row" style={{ gap: 6 }}>
                    <b>{o.name}</b>
                    {o.id === me?.id && <Tag color="processing">你</Tag>}
                  </div>
                  <div className="ops-sub ops-mono">{o.account}</div>
                </div>
              </div>
            ),
          },
          { title: "角色", render: (_, o) => <Tag color={o.role === "ADMIN" ? "processing" : undefined}>{roleLabel(o.role)}</Tag> },
          {
            title: "状态",
            render: (_, o) => (
              <span className="ops-row" style={{ gap: 4, flexWrap: "wrap" }}>
                {o.status === "active" ? <Tag color="success">正常</Tag> : <Tag>已停用</Tag>}
                {o.locked && (
                  <Tooltip title="试错太多次，过一会儿自动解锁；重置密码也会解锁">
                    <Tag color="error">暂时锁定</Tag>
                  </Tooltip>
                )}
                {o.must_change_password && <Tag color="warning">待改临时密码</Tag>}
              </span>
            ),
          },
          { title: "最近登录", dataIndex: "last_login_at", render: (v: string | null) => <span className="ops-muted">{v ? dateTime(v) : "还没登录过"}</span> },
          {
            title: "",
            width: 64,
            align: "right",
            render: (_, o) =>
              o.id === me?.id ? (
                <span className="ops-sub ops-nw">自己的账号在右上角改密码</span>
              ) : (
                <Dropdown
                  trigger={["click"]}
                  placement="bottomRight"
                  menu={{
                    items: [
                      {
                        key: "role",
                        icon: <Icon name="shield" size={14} />,
                        label: o.role === "ADMIN" ? "改为运营观察员" : "改为运营管理员",
                        disabled: !admin,
                      },
                      { key: "reset", icon: <Icon name="key" size={14} />, label: "重置密码", disabled: !admin },
                      { type: "divider" },
                      o.status === "active"
                        ? { key: "disable", icon: <Icon name="ban" size={14} />, label: "停用", danger: true, disabled: !admin }
                        : { key: "enable", icon: <Icon name="power" size={14} />, label: "启用", disabled: !admin },
                    ],
                    onClick: ({ key }) => {
                      if (key === "role") {
                        const role: Role = o.role === "ADMIN" ? "OBSERVER" : "ADMIN";
                        void act(() => api.patch(`/operators/${o.id}`, { role }), `已改为${roleLabel(role)}`);
                      }
                      if (key === "reset") reset(o);
                      if (key === "disable") void act(() => api.post(`/operators/${o.id}/disable`), "已停用，他正在用的登录立即失效");
                      if (key === "enable") void act(() => api.post(`/operators/${o.id}/enable`), "已启用");
                    },
                  }}
                >
                  <Button type="text" size="small" aria-label="更多" icon={<Icon name="more" />} />
                </Dropdown>
              ),
          },
        ]}
      />
      {creating && (
        <CreateOperator
          onClose={() => setCreating(false)}
          onCreated={(account, value) => {
            setSecret({ title: "账号已开", account, value, warn: "临时密码只显示这一次。他第一次登录要改成自己的密码" });
            void qc.invalidateQueries({ queryKey: ["operators"] });
          }}
        />
      )}
      {secret && (
        <Modal
          open
          title={secret.title}
          maskClosable={false}
          onCancel={() => setSecret(null)}
          footer={
            <Button type="primary" onClick={() => setSecret(null)}>
              完成
            </Button>
          }
        >
          <OnceBox
            rows={[
              ["登录地址", `${window.location.origin}${BASE_PATH}/login`],
              ["账号", secret.account],
              ["临时密码", secret.value],
            ]}
            warn={secret.warn}
          />
        </Modal>
      )}
    </>
  );
}

function CreateOperator({ onClose, onCreated }: { onClose: () => void; onCreated: (account: string, temp: string) => void }) {
  const [form] = Form.useForm();
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const role = (Form.useWatch("role", form) as Role | undefined) ?? "OBSERVER";
  return (
    <Modal open title="开运营账号" okText="开账号" onCancel={onClose} onOk={() => form.submit()} confirmLoading={busy}>
      <ErrorBox error={error} />
      <Form
        form={form}
        layout="vertical"
        requiredMark={false}
        initialValues={{ role: "OBSERVER" }}
        onFinish={async (v) => {
          setBusy(true);
          try {
            const out = await api.post<{ operator: Operator; temp_password: string }>("/operators", v);
            onCreated(out.operator.account, out.temp_password);
            onClose();
          } catch (e) {
            setError(e);
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="ops-cols-2">
          <Form.Item name="account" label="账号" extra="小写字母、数字、点和下划线，3–32 位" rules={[{ required: true, message: "请输入账号" }]}>
            <Input className="ops-mono" />
          </Form.Item>
          <Form.Item name="name" label="名字" rules={[{ required: true, message: "请输入名字" }]}>
            <Input />
          </Form.Item>
        </div>
        <Form.Item name="role" hidden>
          <Input />
        </Form.Item>
        <Form.Item label="角色">
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {ROLES.map((r) => (
              <div
                key={r.value}
                role="radio"
                aria-checked={role === r.value}
                tabIndex={0}
                className="ops-radio-card"
                onClick={() => form.setFieldValue("role", r.value)}
                onKeyDown={(e) => e.key === "Enter" && form.setFieldValue("role", r.value)}
              >
                <span className="dot" />
                <div>
                  <b>{r.label}</b>
                  <div className="ops-sub">{r.desc}</div>
                </div>
              </div>
            ))}
          </div>
        </Form.Item>
      </Form>
    </Modal>
  );
}
