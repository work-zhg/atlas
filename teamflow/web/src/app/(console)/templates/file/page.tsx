"use client";

import { PlusOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Form, Input, Modal, Segmented, Select, Space, Table } from "antd";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { PageHead, StatusTag } from "@/components/common";
import { api, errorText } from "@/lib/api";
import { dt, USAGE } from "@/lib/format";
import { useCan } from "@/lib/session";
import type { FileTemplate, Usage } from "@/lib/types";

export default function FileTemplatesPage() {
  const can = useCan();
  const [usage, setUsage] = useState<string>("");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const { data = [], isLoading } = useQuery({
    queryKey: ["file-templates", usage, q],
    queryFn: () => api.get<FileTemplate[]>("/file-templates", { usage, q }),
  });
  return (
    <>
      <PageHead
        title="文件模板"
        sub="产物与评审规则的 Markdown 模板；流程节点引用它，Agent 按模板产出"
        extra={
          can("file_template:manage") && (
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
              新建文件模板
            </Button>
          )
        }
      />
      <Space style={{ marginBottom: 12 }}>
        <Segmented
          value={usage}
          onChange={(v) => setUsage(String(v))}
          options={[{ value: "", label: "全部" }, ...Object.entries(USAGE).map(([value, label]) => ({ value, label }))]}
        />
        <Input.Search placeholder="搜索名称" allowClear onSearch={setQ} style={{ width: 220 }} />
      </Space>
      <Table<FileTemplate>
        rowKey="id"
        loading={isLoading}
        dataSource={data}
        pagination={false}
        style={{ background: "#fff" }}
        columns={[
          {
            title: "名称",
            render: (_, t) => (
              <Link href={`/templates/file/${t.id}`}>
                {t.icon} {t.name}
              </Link>
            ),
          },
          { title: "用途", width: 110, render: (_, t) => USAGE[t.usage] },
          {
            title: "生效版本",
            width: 110,
            render: (_, t) => (t.current_version ? `v${t.current_version.version_no}` : <span className="tf-muted">未上传</span>),
          },
          { title: "被引用", width: 90, dataIndex: "reference_count" },
          { title: "状态", width: 90, render: (_, t) => <StatusTag status={t.status} /> },
          { title: "更新时间", width: 160, render: (_, t) => dt(t.updated_at) },
        ]}
      />
      <CreateFileTemplate open={open} onClose={() => setOpen(false)} />
    </>
  );
}

function CreateFileTemplate({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const router = useRouter();
  const { message } = App.useApp();
  const [form] = Form.useForm();
  async function submit() {
    const v = await form.validateFields();
    try {
      const t = await api.post<FileTemplate>("/file-templates", v);
      await qc.invalidateQueries({ queryKey: ["file-templates"] });
      onClose();
      router.push(`/templates/file/${t.id}`);
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <Modal open={open} title="新建文件模板" onOk={submit} onCancel={onClose} destroyOnHidden>
      <Form form={form} layout="vertical" requiredMark={false} initialValues={{ usage: "artifact" as Usage, icon: "📄" }}>
        <Form.Item name="name" label="名称" rules={[{ required: true, message: "请填写名称" }]}>
          <Input maxLength={64} placeholder="如：需求文档模板" />
        </Form.Item>
        <Space>
          <Form.Item name="icon" label="图标">
            <Input maxLength={4} style={{ width: 80 }} />
          </Form.Item>
          <Form.Item name="usage" label="用途">
            <Select style={{ width: 160 }} options={Object.entries(USAGE).map(([value, label]) => ({ value, label }))} />
          </Form.Item>
        </Space>
        <Form.Item name="description" label="说明">
          <Input.TextArea maxLength={2000} rows={3} />
        </Form.Item>
      </Form>
    </Modal>
  );
}
