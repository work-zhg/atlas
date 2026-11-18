"use client";

/**
 * 权限 › 岗位：岗位 = 一组默认角色。把岗位授给人或部门，拿到岗位的人自动拥有它的默认角色。
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, Dropdown, Form, Input, Modal, Space, Table, Tag } from "antd";
import { useState } from "react";

import { PageHead } from "@/components/common";
import { GrantToModal } from "@/components/grantActions";
import { RoleChecks, usePositions } from "@/components/grantPickers";
import { Icon } from "@/components/Icon";
import { api, errorText } from "@/lib/api";
import { useCan } from "@/lib/session";
import type { PositionRow } from "@/lib/types";

export default function PositionsPage() {
  const can = useCan();
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const list = usePositions();
  const [form, setForm] = useState<{ open: boolean; pos?: PositionRow | null }>({ open: false });
  const [grantTo, setGrantTo] = useState<PositionRow | null>(null);
  const del = useMutation({
    mutationFn: (id: string) => api.del(`/positions/${id}`),
    onSuccess: () => (qc.invalidateQueries({ queryKey: ["positions"] }), message.success("已删除")),
    onError: (e) => message.error(errorText(e)),
  });
  return (
    <>
      <PageHead
        title="岗位"
        sub="岗位 = 一组默认角色。把岗位授给人或部门，拿到岗位的人自动拥有它的默认角色。"
        extra={
          can("position:manage") && (
            <Button type="primary" icon={<Icon name="plus" size={14} />} onClick={() => setForm({ open: true, pos: null })}>
              新建岗位
            </Button>
          )
        }
      />
      <Card styles={{ body: { padding: 0 } }}>
        <Table<PositionRow>
          rowKey="id"
          loading={list.isLoading}
          dataSource={list.data ?? []}
          pagination={false}
          locale={{ emptyText: "还没有岗位" }}
          columns={[
            {
              title: "岗位",
              render: (_, p) => (
                <>
                  <b className="uc-nw">{p.name}</b>
                  <div className="uc-sub uc-mono">{p.code}</div>
                </>
              ),
            },
            {
              title: "默认角色",
              render: (_, p) =>
                p.roles.length ? (
                  <Space size={[4, 4]} wrap>
                    {p.roles.map((r) => (
                      <Tag key={r.id} color="purple" style={{ marginInlineEnd: 0 }}>
                        {r.app} · {r.name}
                      </Tag>
                    ))}
                  </Space>
                ) : (
                  <span className="uc-muted">无</span>
                ),
            },
            {
              title: "授予给",
              render: (_, p) =>
                p.grants.length ? (
                  <span>
                    {p.grants
                      .slice(0, 2)
                      .map((g) => `${g.subject.name}${g.include_sub ? "（含下级）" : ""}`)
                      .join("、")}
                    {p.grants.length > 2 && <span className="uc-muted"> 等 {p.grants.length} 个</span>}
                  </span>
                ) : (
                  <span className="uc-muted">尚未授予</span>
                ),
            },
            { title: "持有人", render: (_, p) => <span className="uc-nw">{p.holders} 人</span> },
            {
              title: "",
              align: "right",
              render: (_, p) => (
                <span className="uc-nw">
                  {can("grant:manage") && (
                    <Button type="text" size="small" onClick={() => setGrantTo(p)}>
                      授予
                    </Button>
                  )}
                  {can("position:manage") && (
                    <Button type="text" size="small" onClick={() => setForm({ open: true, pos: p })}>
                      编辑
                    </Button>
                  )}
                  {can("position:manage") && (
                    <Dropdown
                      trigger={["click"]}
                      menu={{
                        items: [
                          {
                            key: "del",
                            danger: true,
                            icon: <Icon name="trash" size={14} />,
                            label: "删除",
                            onClick: () =>
                              modal.confirm({
                                title: `删除岗位 · ${p.name}`,
                                content: `持有该岗位的 ${p.holders} 人将失去它的默认角色（直接授予的角色不受影响）。`,
                                okText: "删除",
                                okButtonProps: { danger: true },
                                onOk: () => del.mutateAsync(p.id),
                              }),
                          },
                        ],
                      }}
                    >
                      <Button type="text" size="small" aria-label="更多" icon={<Icon name="more" />} />
                    </Dropdown>
                  )}
                </span>
              ),
            },
          ]}
        />
      </Card>
      {form.open && <PositionForm pos={form.pos ?? null} onClose={() => setForm({ open: false })} />}
      {grantTo && <GrantToModal kind="position" target={grantTo} onClose={() => setGrantTo(null)} />}
    </>
  );
}

function PositionForm({ pos, onClose }: { pos: PositionRow | null; onClose: () => void }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [roles, setRoles] = useState<string[]>(pos?.roles.map((r) => r.id) ?? []);
  const [form] = Form.useForm();
  async function submit(v: { code: string; name: string }) {
    try {
      if (pos) await api.patch(`/positions/${pos.id}`, { name: v.name, role_ids: roles, version: pos.version });
      else await api.post("/positions", { code: v.code, name: v.name, role_ids: roles });
      qc.invalidateQueries({ queryKey: ["positions"] });
      message.success("岗位已保存");
      onClose();
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <Modal open title={pos ? `编辑岗位 · ${pos.name}` : "新建岗位"} onCancel={onClose} onOk={() => form.submit()} okText="保存" width={620}>
      <Form form={form} layout="vertical" onFinish={submit} initialValues={pos ? { name: pos.name, code: pos.code } : {}}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <Form.Item name="name" label="岗位名称" rules={[{ required: true }]}>
            <Input maxLength={32} />
          </Form.Item>
          <Form.Item name="code" label="岗位编码" rules={[{ required: true, pattern: /^[A-Z][A-Z0-9_]{1,31}$/, message: "大写字母开头，大写字母、数字、下划线" }]}>
            <Input disabled={!!pos} placeholder="如 BACKEND_ENGINEER" />
          </Form.Item>
        </div>
        <div style={{ fontWeight: 600, marginBottom: 2 }}>默认角色</div>
        <div className="uc-sub" style={{ marginBottom: 8 }}>
          拿到这个岗位的人（含通过部门拿到的）自动拥有这些角色；调整后立即生效
        </div>
        <RoleChecks value={roles} onChange={setRoles} />
      </Form>
    </Modal>
  );
}
