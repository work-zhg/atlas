"use client";

import { PlusOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, Form, Input, Modal, Select, Space, Spin, Tag } from "antd";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { EmptyBox, PageHead, StatusTag } from "@/components/common";
import { api, errorText } from "@/lib/api";
import { dt } from "@/lib/format";
import { useCan } from "@/lib/session";
import type { FlowTemplate } from "@/lib/types";

export default function FlowTemplatesPage() {
  const can = useCan();
  const [open, setOpen] = useState(false);
  const { data = [], isLoading } = useQuery({ queryKey: ["flow-templates"], queryFn: () => api.get<FlowTemplate[]>("/flow-templates") });
  return (
    <>
      <PageHead
        title="流程模板"
        sub="定义一类工作怎么做：节点、执行角色、产物与评审；项目绑定模板后把角色分配给人和 Agent"
        extra={
          can("flow_template:manage") && (
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
              新建流程模板
            </Button>
          )
        }
      />
      {isLoading ? (
        <Spin />
      ) : !data.length ? (
        <EmptyBox text="还没有流程模板" />
      ) : (
        <div className="tf-card-grid">
          {data.map((t) => (
            <Link key={t.id} href={`/templates/flow/${t.id}`} style={{ textDecoration: "none" }}>
              <Card hoverable style={{ opacity: t.status === "disabled" ? 0.65 : 1 }}>
                <Space style={{ width: "100%", justifyContent: "space-between" }}>
                  <span style={{ fontSize: 15, fontWeight: 650 }}>
                    {t.icon ?? "🧭"} {t.name}
                  </span>
                  <Space size={4}>
                    {t.current_version ? <Tag color="blue">{t.current_version.label}</Tag> : <Tag>未发布</Tag>}
                    {t.has_draft && can("flow_template:manage") && <Tag color="orange">草稿</Tag>}
                    {t.status === "disabled" && <StatusTag status="disabled" />}
                  </Space>
                </Space>
                <div className="tf-muted" style={{ margin: "6px 0 12px", minHeight: 40, fontSize: 13 }}>
                  {t.description || "—"}
                </div>
                <Space size={16} className="tf-muted" style={{ fontSize: 12.5 }}>
                  <span>🔷 {t.node_count} 节点</span>
                  <span>📁 {t.project_count} 项目在用</span>
                  <span>{dt(t.updated_at)}</span>
                </Space>
              </Card>
            </Link>
          ))}
        </div>
      )}
      <CreateFlowTemplate open={open} onClose={() => setOpen(false)} templates={data} />
    </>
  );
}

function CreateFlowTemplate({ open, onClose, templates }: { open: boolean; onClose: () => void; templates: FlowTemplate[] }) {
  const qc = useQueryClient();
  const router = useRouter();
  const { message } = App.useApp();
  const [form] = Form.useForm();
  async function submit() {
    const v = await form.validateFields();
    try {
      const t = await api.post<FlowTemplate>("/flow-templates", v);
      await qc.invalidateQueries({ queryKey: ["flow-templates"] });
      onClose();
      router.push(`/templates/flow/${t.id}/edit`);
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <Modal open={open} title="新建流程模板" onOk={submit} onCancel={onClose} destroyOnHidden>
      <Form form={form} layout="vertical" requiredMark={false} initialValues={{ icon: "🧭" }}>
        <Form.Item name="name" label="名称" rules={[{ required: true, message: "请填写名称" }]}>
          <Input maxLength={64} placeholder="如：标准研发流程" />
        </Form.Item>
        <Form.Item name="icon" label="图标">
          <Input maxLength={4} style={{ width: 80 }} />
        </Form.Item>
        <Form.Item name="description" label="说明">
          <Input.TextArea maxLength={2000} rows={2} />
        </Form.Item>
        <Form.Item name="scope" label="适用范围">
          <Input maxLength={200} placeholder="如：中大型需求" />
        </Form.Item>
        <Form.Item name="copy_from" label="复制自（可选）">
          <Select
            allowClear
            placeholder="从已发布模板的当前版本复制结构"
            options={templates.filter((t) => t.current_version).map((t) => ({ value: t.id, label: `${t.name} · ${t.current_version!.label}` }))}
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}
