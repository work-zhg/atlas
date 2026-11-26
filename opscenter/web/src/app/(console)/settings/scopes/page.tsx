"use client";

/**
 * 记忆作用域：产品自带的定义（所有企业共用），决定记忆按什么字段隔离、要不要校验数据权限。
 * 关联主键的字段建好后不能改。打开数据权限开关后，它的数据编码要在「应用目录 › 记忆服务」里录入。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Empty, Form, Input, Modal, Switch, Table, Tag, Tooltip } from "antd";
import { useState } from "react";

import { ErrorBox, OBSERVER_TIP, PageHead, errorText } from "@/components/common";
import { Icon } from "@/components/Icon";
import { sys } from "@/lib/api";
import { dateTime } from "@/lib/format";
import { useAccess, useSystems, withCapability } from "@/lib/session";
import type { MemoryScope } from "@/lib/types";

export default function ScopesPage() {
  const { data: systems } = useSystems();
  const memory = withCapability(systems, "memory_scopes")[0];
  if (systems && !memory)
    return (
      <>
        <PageHead title="记忆作用域" />
        <Empty description="没有登记记忆服务" />
      </>
    );
  return memory ? <Scopes code={memory.code} /> : null;
}

function Scopes({ code }: { code: string }) {
  const ops = sys(code);
  const qc = useQueryClient();
  const { message } = App.useApp();
  // 这个系统的运营模块：改要读写
  const acc = useAccess();
  const admin = acc.write(acc.moduleOf(code));
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
        sub="记忆按作用域分开存：一个作用域由哪几个字段确定一份记忆，读写时要不要按数据权限校验。产品自带的定义，所有企业共用"
        extra={
          <Tooltip title={admin ? undefined : OBSERVER_TIP}>
            <Button type="primary" icon={<Icon name="plus" />} disabled={!admin} onClick={() => setEdit("new")}>
              新建作用域
            </Button>
          </Tooltip>
        }
      />
      <ErrorBox error={q.error} />
      <Table<MemoryScope>
        rowKey="code"
        loading={q.isLoading}
        dataSource={q.data?.data ?? []}
        pagination={false}
        columns={[
          { title: "编码", dataIndex: "code", render: (v: string) => <b className="ops-mono">{v}</b> },
          { title: "说明", dataIndex: "description", render: (v: string) => v || <span className="ops-muted">—</span> },
          {
            title: "关联主键",
            dataIndex: "key_fields",
            render: (v: string[]) => (
              <span className="ops-row" style={{ gap: 4, flexWrap: "wrap" }}>
                {v.map((f, i) => (
                  <span key={f} className="ops-row" style={{ gap: 4 }}>
                    {i > 0 && <span className="ops-muted">+</span>}
                    <Tag className="ops-mono">{f}</Tag>
                  </span>
                ))}
              </span>
            ),
          },
          {
            title: "数据权限",
            render: (_, s) => (s.check_permission ? <Tag color="processing">校验 · {s.data_code}</Tag> : <Tag>不校验</Tag>),
          },
          { title: "更新时间", dataIndex: "updated_at", render: (v: string) => <span className="ops-muted">{dateTime(v)}</span> },
          {
            title: "",
            align: "right",
            render: (_, s) => (
              <Button type="link" size="small" disabled={!admin} onClick={() => setEdit(s)}>
                编辑
              </Button>
            ),
          },
        ]}
      />
      {edit && (
        <Modal open title={edit === "new" ? "新建作用域" : `编辑 ${edit.code}`} okText="保存" onCancel={() => setEdit(null)} onOk={() => form.submit()}>
          <Form
            form={form}
            layout="vertical"
            requiredMark={false}
            initialValues={edit === "new" ? { check_permission: false } : { ...edit, key_fields: edit.key_fields.join(", ") }}
            onFinish={submit}
          >
            <div className="ops-cols-2">
              <Form.Item name="code" label="编码" rules={[{ required: true, message: "请输入编码" }]}>
                <Input className="ops-mono" disabled={edit !== "new"} placeholder="如 user_project" />
              </Form.Item>
              <Form.Item name="description" label="说明">
                <Input />
              </Form.Item>
            </div>
            <Form.Item name="key_fields" label="关联主键（有序，逗号分隔）" extra="由哪几个字段确定一份记忆；建好后不能改" rules={[{ required: true, message: "请填写关联主键" }]}>
              <Input className="ops-mono" disabled={edit !== "new"} placeholder="uid, project" />
            </Form.Item>
            <Form.Item name="check_permission" label="读写时校验数据权限" valuePropName="checked">
              <Switch />
            </Form.Item>
            <Form.Item name="data_code" label="数据编码" extra="不填按编码生成（user_project → MemoryUserProject）；要在「应用目录 › 记忆服务」里录入">
              <Input className="ops-mono" />
            </Form.Item>
          </Form>
        </Modal>
      )}
    </>
  );
}
