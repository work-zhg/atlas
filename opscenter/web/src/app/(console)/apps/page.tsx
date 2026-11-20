"use client";

/**
 * 应用目录：接入了哪些应用。操作码、菜单、数据编码、应用内置角色由运营人员在这里录入（各应用代码里
 * 不登记，清单见各服务 README「用户中心登记」）；数据存在用户中心，经它的运营接口改。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Form, Input, Modal, Table, Tag } from "antd";
import Link from "next/link";
import { useState } from "react";

import { ErrorBox, OneTimeSecret, PageHead } from "@/components/common";
import { UC, sys } from "@/lib/api";
import { dateTime } from "@/lib/format";
import { useMe } from "@/lib/session";
import type { AppInfo } from "@/lib/types";

export default function AppsPage() {
  const { data: me } = useMe();
  const q = useQuery({ queryKey: ["apps"], queryFn: () => sys(UC).get<AppInfo[]>("/apps") });
  const [open, setOpen] = useState(false);
  return (
    <>
      <PageHead
        title="应用目录"
        sub="所有租户共用的应用定义。接入后要在租户详情里给租户开通，租户管理员再定哪些人能用"
        extra={
          me?.role === "ADMIN" && (
            <Button type="primary" onClick={() => setOpen(true)}>
              接入应用
            </Button>
          )
        }
      />
      <ErrorBox error={q.error} />
      <Table<AppInfo>
        rowKey="id"
        loading={q.isLoading}
        dataSource={q.data ?? []}
        pagination={false}
        columns={[
          {
            title: "应用",
            render: (_, a) => (
              <Link href={`/apps/${a.id}`}>
                {a.icon} {a.name}
              </Link>
            ),
          },
          { title: "编码", dataIndex: "code", render: (v: string | null) => v ?? "—" },
          {
            title: "状态",
            render: (_, a) => (
              <>
                <Tag color={a.status === "active" ? "green" : "default"}>{a.status === "active" ? "启用" : "停用"}</Tag>
                {a.is_builtin && <Tag>产品自带</Tag>}
              </>
            ),
          },
          { title: "操作码", dataIndex: "operations" },
          { title: "菜单", dataIndex: "menus" },
          { title: "内置角色", dataIndex: "roles" },
          { title: "数据编码", dataIndex: "data_types" },
          { title: "入口地址", dataIndex: "home_url", render: (v: string | null) => v ?? "—" },
          { title: "接入时间", dataIndex: "created_at", render: dateTime },
        ]}
      />
      {open && <CreateApp onClose={() => setOpen(false)} />}
    </>
  );
}

function CreateApp({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [form] = Form.useForm();
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [secret, setSecret] = useState<string | null>(null);

  async function submit(v: Record<string, string>) {
    setBusy(true);
    setError(null);
    try {
      const out = await sys(UC).post<{ app: AppInfo; app_secret: string }>("/apps", v);
      setSecret(out.app_secret);
      await qc.invalidateQueries({ queryKey: ["apps"] });
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  if (secret)
    return (
      <Modal open title="应用已接入" onCancel={onClose} footer={<Button onClick={onClose}>完成</Button>}>
        <OneTimeSecret label="App Secret（经网关接入的外部应用用；内网应用用应用编码即可）" value={secret} />
      </Modal>
    );

  return (
    <Modal open title="接入应用" onCancel={onClose} onOk={() => form.submit()} confirmLoading={busy}>
      <ErrorBox error={error} />
      <Form form={form} layout="vertical" requiredMark={false} onFinish={submit}>
        <Form.Item name="name" label="名称" rules={[{ required: true }]}>
          <Input />
        </Form.Item>
        <Form.Item
          name="code"
          label="应用编码"
          extra="内网调用户中心开放接口时请求头 X-Caller-App 写它，和应用的配置一致（如 atlas、teamflow）"
          rules={[{ pattern: /^[a-z][a-z0-9-]{1,31}$/, message: "小写字母开头，小写字母、数字、短横线" }]}
        >
          <Input />
        </Form.Item>
        <Form.Item name="icon" label="图标" extra="一个 emoji">
          <Input maxLength={4} />
        </Form.Item>
        <Form.Item name="home_url" label="入口地址" extra="站内路径（/atlas）或完整地址；不填 = 不在应用切换里出现">
          <Input />
        </Form.Item>
        <Form.Item name="description" label="说明">
          <Input.TextArea rows={2} />
        </Form.Item>
      </Form>
    </Modal>
  );
}
