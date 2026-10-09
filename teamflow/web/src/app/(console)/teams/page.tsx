"use client";

import { PlusOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Avatar, Button, Card, Form, Input, Modal, Space, Spin, Tag } from "antd";
import Link from "next/link";
import { useState } from "react";

import { AgentAvatar, DeptPicker, EmptyBox, LevelTag, PageHead, UserPicker } from "@/components/common";
import { api, errorText } from "@/lib/api";
import { useCan } from "@/lib/session";
import type { Team } from "@/lib/types";

export default function TeamsPage() {
  const can = useCan();
  const [open, setOpen] = useState(false);
  const { data, isLoading } = useQuery({ queryKey: ["teams"], queryFn: () => api.get<Team[]>("/teams") });

  return (
    <>
      <PageHead
        title="团队"
        sub={can("team:manage_all") ? "全部团队（平台管理员）" : "我所在的团队"}
        extra={
          can("team:create") && (
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
              新建团队
            </Button>
          )
        }
      />
      {isLoading ? (
        <Spin />
      ) : !data?.length ? (
        <EmptyBox text={can("team:create") ? "还没有团队，新建一个吧" : "你还不在任何团队中，请联系团队管理员把你加入团队"} />
      ) : (
        <div className="tf-card-grid">
          {data.map((t) => (
            <Link key={t.id} href={`/teams/${t.id}`} style={{ textDecoration: "none" }}>
              <Card hoverable>
                <Space style={{ width: "100%", justifyContent: "space-between" }}>
                  <span style={{ fontSize: 16, fontWeight: 650 }}>{t.name}</span>
                  {t.my_level && t.my_level !== "NONE" ? <LevelTag level={t.my_level} /> : <Tag>未加入</Tag>}
                </Space>
                <div className="tf-muted" style={{ margin: "6px 0 14px", minHeight: 22, fontSize: 13 }}>
                  {t.description || "—"}
                </div>
                <Space size={18} className="tf-muted" style={{ fontSize: 12.5 }}>
                  <span>👤 {t.member_count} 成员</span>
                  <span>🤖 {t.agent_count} Agent</span>
                  <span>📁 {t.project_count} 项目</span>
                </Space>
                {!!t.agents?.length && (
                  <Avatar.Group style={{ marginTop: 12, display: "flex" }}>
                    {t.agents.map((a) => (
                      <AgentAvatar key={a.name} name={a.name} available={a.available} size={26} />
                    ))}
                  </Avatar.Group>
                )}
              </Card>
            </Link>
          ))}
        </div>
      )}
      <CreateTeam open={open} onClose={() => setOpen(false)} />
    </>
  );
}

function CreateTeam({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  async function submit() {
    const v = await form.validateFields();
    setSaving(true);
    try {
      const members = [
        ...(v.users ?? []).map((id: string) => ({ type: "USER", id })),
        ...(v.depts ?? []).map((id: string) => ({ type: "DEPT", id, include_sub: true })),
      ];
      const t = await api.post<Team>("/teams", { name: v.name, description: v.description, admins: v.admins, members });
      await qc.invalidateQueries({ queryKey: ["teams"] });
      message.success("团队已创建");
      if (t.warnings?.length) modal.warning({ title: "请注意", content: t.warnings.join("\n") });
      form.resetFields();
      onClose();
    } catch (e) {
      message.error(errorText(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal open={open} title="新建团队" onOk={submit} onCancel={onClose} confirmLoading={saving} destroyOnHidden width={560}>
      <Form form={form} layout="vertical" requiredMark={false}>
        <Form.Item name="name" label="团队名称" rules={[{ required: true, message: "请填写团队名称" }]}>
          <Input maxLength={64} placeholder="如：交易中台" />
        </Form.Item>
        <Form.Item name="description" label="说明">
          <Input.TextArea maxLength={500} rows={2} />
        </Form.Item>
        <Form.Item
          name="admins"
          label="团队管理员"
          extra="对该团队拥有 Owner 权限：配置项目、管理成员与 Agent"
          rules={[{ required: true, message: "至少指定 1 名团队管理员" }]}
        >
          <UserPicker />
        </Form.Item>
        <Form.Item name="users" label="初始成员（个人）">
          <UserPicker />
        </Form.Item>
        <Form.Item name="depts" label="初始成员（部门，含下级）">
          <DeptPicker />
        </Form.Item>
      </Form>
    </Modal>
  );
}
