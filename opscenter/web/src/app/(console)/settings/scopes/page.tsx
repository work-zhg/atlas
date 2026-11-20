"use client";

/**
 * 记忆作用域：产品自带的定义（所有租户共用），决定记忆按什么字段隔离、要不要校验数据权限。
 * 关联主键的字段建好后不能改。打开数据权限开关后，它的数据编码要在「应用目录 › 记忆服务」里录入。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Form, Input, Modal, Switch, Table, Tag, Typography } from "antd";
import { useState } from "react";

import { ErrorBox, PageHead, errorText } from "@/components/common";
import { sys } from "@/lib/api";
import { dateTime } from "@/lib/format";
import { useMe, useSystems, withCapability } from "@/lib/session";
import type { MemoryScope } from "@/lib/types";

export default function ScopesPage() {
  const { data: systems } = useSystems();
  const memory = withCapability(systems, "memory_scopes")[0];
  if (!memory) return <PageHead title="记忆作用域" sub="没有登记记忆服务" />;
  return <Scopes code={memory.code} />;
}

function Scopes({ code }: { code: string }) {
  const ops = sys(code);
  const qc = useQueryClient();
  const { message } = App.useApp();
  const { data: me } = useMe();
  const admin = me?.role === "ADMIN";
  const key = ["scopes", code];
  const q = useQuery({ queryKey: key, queryFn: () => ops.get<{ data: MemoryScope[] }>("/scopes") });
  const [edit, setEdit] = useState<MemoryScope | "new" | null>(null);
  const [form] = Form.useForm();

  async function submit(v: { code?: string; description?: string; key_fields?: string; check_permission?: boolean; data_code?: string }) {
    try {
      if (edit === "new") {
        await ops.post("/scopes", {
          code: v.code,
          description: v.description ?? "",
          key_fields: (v.key_fields ?? "").split(/[,，\s]+/).filter(Boolean),
          check_permission: !!v.check_permission,
          data_code: v.data_code || null,
        });
      } else if (edit) {
        await ops.patch(`/scopes/${edit.code}`, {
          description: v.description,
          check_permission: v.check_permission,
          data_code: v.data_code || null,
        });
      }
      message.success("已保存");
      setEdit(null);
      await qc.invalidateQueries({ queryKey: key });
    } catch (e) {
      message.error(errorText(e));
    }
  }

  return (
    <>
      <PageHead
        title="记忆作用域"
        sub="产品自带的定义，所有租户共用"
        extra={
          admin && (
            <Button type="primary" onClick={() => setEdit("new")}>
              新建作用域
            </Button>
          )
        }
      />
      <ErrorBox error={q.error} />
      <Table<MemoryScope>
        rowKey="code"
        loading={q.isLoading}
        dataSource={q.data?.data ?? []}
        pagination={false}
        columns={[
          { title: "编码", dataIndex: "code", render: (v: string) => <Typography.Text code>{v}</Typography.Text> },
          { title: "说明", dataIndex: "description" },
          { title: "关联主键", dataIndex: "key_fields", render: (v: string[]) => v.join(" + ") },
          {
            title: "数据权限",
            render: (_, s) => (s.check_permission ? <Tag color="blue">校验 · {s.data_code}</Tag> : <Tag>不校验</Tag>),
          },
          { title: "更新时间", dataIndex: "updated_at", render: dateTime },
          {
            title: "",
            render: (_, s) =>
              admin && (
                <Button type="link" size="small" onClick={() => setEdit(s)}>
                  编辑
                </Button>
              ),
          },
        ]}
      />
      {edit && (
        <Modal open title={edit === "new" ? "新建作用域" : `编辑 ${edit.code}`} onCancel={() => setEdit(null)} onOk={() => form.submit()}>
          <Form
            form={form}
            layout="vertical"
            initialValues={edit === "new" ? { check_permission: false } : { ...edit, key_fields: edit.key_fields.join(", ") }}
            onFinish={submit}
          >
            <Form.Item name="code" label="编码" rules={[{ required: true }]}>
              <Input disabled={edit !== "new"} />
            </Form.Item>
            <Form.Item name="description" label="说明">
              <Input />
            </Form.Item>
            <Form.Item name="key_fields" label="关联主键的字段（逗号分隔，有序）" extra="建好后不能改" rules={[{ required: true }]}>
              <Input disabled={edit !== "new"} placeholder="uid, project" />
            </Form.Item>
            <Form.Item name="check_permission" label="校验数据权限" valuePropName="checked">
              <Switch />
            </Form.Item>
            <Form.Item name="data_code" label="数据编码" extra="不填按编码生成（user_project → MemoryUserProject）">
              <Input />
            </Form.Item>
          </Form>
        </Modal>
      )}
    </>
  );
}
