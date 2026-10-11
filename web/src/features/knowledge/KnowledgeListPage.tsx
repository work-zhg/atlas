"use client";

/** 知识库列表（doc/atlas-rag-design.html §13）。卡片 + 新建弹窗。 */
import { App, Button, Card, Empty, Form, Input, Modal, Radio, Skeleton, Switch, Tag, Typography } from "antd";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Icon } from "@/components/Icon";
import { can, useMe } from "@/lib/session";

import { type DocForm, type Permission, PERMISSION_LABEL, kbApi, useDatasets, useKbMutation } from "./api";

export function KnowledgeListPage() {
  const { data: me } = useMe();
  const [keyword, setKeyword] = useState("");
  const [creating, setCreating] = useState(false);
  const { data, isPending, error } = useDatasets(keyword || undefined);

  return (
    <div style={{ flex: 1, overflow: "auto", padding: "24px 28px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 16 }}>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>
            知识库
          </Typography.Title>
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            给智能体提供领域知识：上传文档，分段建索引，绑定到智能体后在对话中检索引用。
          </Typography.Text>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <Input.Search placeholder="搜索知识库" allowClear onSearch={setKeyword} style={{ width: 220 }} />
          {can(me, "kb:create") && (
            <Button type="primary" icon={<Icon name="plus" size={14} />} onClick={() => setCreating(true)}>
              新建知识库
            </Button>
          )}
        </div>
      </div>

      {error && <Typography.Text type="danger">{(error as Error).message}</Typography.Text>}
      {isPending ? (
        <Skeleton active />
      ) : !data?.data.length ? (
        <Empty description="还没有可见的知识库" style={{ marginTop: 80 }} />
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: 16 }}>
          {data.data.map((ds) => (
            <Link key={ds.id} href={`/knowledge/${ds.id}`} style={{ color: "inherit" }}>
              <Card hoverable size="small" style={{ height: "100%" }}>
                <div style={{ display: "flex", gap: 12 }}>
                  <div style={{ fontSize: 28, lineHeight: 1 }}>{ds.icon}</div>
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div style={{ fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {ds.name}
                    </div>
                    <div style={{ fontSize: 12, color: "var(--fg-3)", marginTop: 2 }}>
                      {ds.document_count} 份文档 · {ds.word_count.toLocaleString()} 字
                    </div>
                  </div>
                </div>
                <Typography.Paragraph
                  type="secondary"
                  ellipsis={{ rows: 2 }}
                  style={{ fontSize: 13, margin: "10px 0 8px", minHeight: 40 }}
                >
                  {ds.description || "暂无描述"}
                </Typography.Paragraph>
                <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
                  <Tag>{PERMISSION_LABEL[ds.permission]}</Tag>
                  <Tag>{ds.doc_form === "hierarchical_model" ? "父子分段" : "通用分段"}</Tag>
                  {ds.review_required && <Tag color="gold">入库审核</Tag>}
                </div>
              </Card>
            </Link>
          ))}
        </div>
      )}
      <CreateModal open={creating} onClose={() => setCreating(false)} />
    </div>
  );
}

function CreateModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const router = useRouter();
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const create = useKbMutation(kbApi.createDataset);

  async function submit() {
    const v = await form.validateFields();
    try {
      const ds = await create.mutateAsync(v);
      form.resetFields();
      onClose();
      router.push(`/knowledge/${ds.id}`);
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  return (
    <Modal
      open={open}
      title="新建知识库"
      onOk={submit}
      onCancel={onClose}
      confirmLoading={create.isPending}
      destroyOnHidden
      width={560}
    >
      <Form
        form={form}
        layout="vertical"
        initialValues={{ icon: "📚", permission: "only_me" as Permission, doc_form: "text_model" as DocForm, review_required: false }}
      >
        <div style={{ display: "flex", gap: 12 }}>
          <Form.Item name="icon" label="图标" style={{ width: 80 }}>
            <Input maxLength={4} />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: "请填写名称" }]} style={{ flex: 1 }}>
            <Input maxLength={255} placeholder="如：客服 FAQ" />
          </Form.Item>
        </div>
        <Form.Item
          name="description"
          label="描述"
          extra="说明这个库里有什么。多知识库路由时模型据此判断该查哪个库。"
        >
          <Input.TextArea rows={2} maxLength={2000} />
        </Form.Item>
        <Form.Item name="doc_form" label="分段结构" extra="已有文档后不能更改。">
          <Radio.Group>
            <Radio value="text_model">通用：按规则切成段，每段建索引</Radio>
            <Radio value="hierarchical_model">父子：小块匹配、返回所在大块</Radio>
          </Radio.Group>
        </Form.Item>
        <Form.Item name="permission" label="可见范围">
          <Radio.Group
            options={(["only_me", "all_team"] as Permission[]).map((v) => ({ value: v, label: PERMISSION_LABEL[v] }))}
          />
        </Form.Item>
        <Form.Item
          name="review_required"
          label="入库审核"
          valuePropName="checked"
          extra="开启后，新文档审核通过才能被检索。绑定给智能体使用的库建议开启。"
        >
          <Switch />
        </Form.Item>
      </Form>
    </Modal>
  );
}
