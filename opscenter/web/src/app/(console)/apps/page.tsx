"use client";

/** 应用目录：接入用户中心的应用。为企业开通后，企业里的人才能用（产品自带的应用所有企业都能用）。 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Form, Input, Modal, Tag, Tooltip } from "antd";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { AppIcon, ErrorBox, OBSERVER_TIP, OnceBox, PageHead } from "@/components/common";
import { Icon } from "@/components/Icon";
import { UC, sys } from "@/lib/api";
import { useAccess } from "@/lib/session";
import type { AppInfo } from "@/lib/types";

export default function AppsPage() {
  const router = useRouter();
  // 应用目录属于用户中心模块
  const acc = useAccess();
  const admin = acc.write(acc.moduleOf(UC));
  const q = useQuery({ queryKey: ["apps"], queryFn: () => sys(UC).get<AppInfo[]>("/apps") });
  const [open, setOpen] = useState(false);

  return (
    <>
      <PageHead
        title="应用目录"
        sub="接入用户中心的应用。为企业开通后，企业里的人才能用；操作码、菜单、内置角色由应用上报，也可以在这里维护"
        extra={
          <Tooltip title={admin ? undefined : OBSERVER_TIP}>
            <Button type="primary" icon={<Icon name="plus" />} disabled={!admin} onClick={() => setOpen(true)}>
              接入应用
            </Button>
          </Tooltip>
        }
      />
      <ErrorBox error={q.error} />
      <div className="ops-grid">
        {(q.data ?? []).map((a) => (
          <div key={a.id} className="ops-acard" role="link" tabIndex={0} onClick={() => router.push(`/apps/${a.id}`)} onKeyDown={(e) => e.key === "Enter" && router.push(`/apps/${a.id}`)}>
            <div className="ops-row" style={{ gap: 12 }}>
              <AppIcon app={a} size={40} />
              <div className="ops-grow">
                <div className="ops-row" style={{ gap: 6 }}>
                  <b>{a.name}</b>
                  {a.is_builtin && <Tag>产品自带</Tag>}
                  {a.status === "disabled" && <Tag color="error">已停用</Tag>}
                </div>
                <div className="ops-sub ops-mono">{a.code ?? "—"}</div>
              </div>
            </div>
            <div className="desc">{a.description || "没有说明"}</div>
            <div className="foot">
              <span>操作码 {a.operations ?? 0}</span>
              <span>菜单 {a.menus ?? 0}</span>
              <span>角色 {a.roles ?? 0}</span>
              <span className="ops-grow" />
              <span>{a.is_builtin ? "所有企业都能用" : `${a.tenants ?? 0} 个企业开通`}</span>
            </div>
          </div>
        ))}
      </div>
      {open && <CreateApp onClose={() => setOpen(false)} />}
    </>
  );
}

function CreateApp({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [form] = Form.useForm();
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState<{ app: AppInfo; app_secret: string } | null>(null);

  async function submit(v: Record<string, string>) {
    setBusy(true);
    setError(null);
    try {
      const out = await sys(UC).post<{ app: AppInfo; app_secret: string }>("/apps", v);
      setCreated(out);
      await qc.invalidateQueries({ queryKey: ["apps"] });
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  if (created)
    return (
      <Modal
        open
        title="应用已接入"
        maskClosable={false}
        onCancel={onClose}
        footer={
          <Button type="primary" onClick={onClose}>
            完成
          </Button>
        }
      >
        <OnceBox
          rows={[
            ["App Key", created.app.app_key ?? ""],
            ["App Secret", created.app_secret],
          ]}
          warn="App Secret 只显示这一次。经网关接入的外部应用要用它；内网应用用应用编码就行"
        />
      </Modal>
    );

  return (
    <Modal open title="接入应用" okText="接入" onCancel={onClose} onOk={() => form.submit()} confirmLoading={busy}>
      <ErrorBox error={error} />
      <Form form={form} layout="vertical" requiredMark={false} onFinish={submit}>
        <div className="ops-cols-2">
          <Form.Item name="name" label="名称" rules={[{ required: true, message: "请输入名称" }]}>
            <Input placeholder="如 合同审查助手" />
          </Form.Item>
          <Form.Item
            name="code"
            label="应用编码"
            extra="内网调用时请求头 X-Caller-App 写它（如 atlas、teamflow）"
            rules={[{ pattern: /^[a-z][a-z0-9-]{1,31}$/, message: "小写字母开头，小写字母、数字、短横线" }]}
          >
            <Input placeholder="如 contract" />
          </Form.Item>
        </div>
        <Form.Item name="icon" label="图标" extra="一个 emoji，显示在方块里">
          <Input maxLength={4} style={{ width: 120 }} />
        </Form.Item>
        <Form.Item name="home_url" label="入口地址" extra="站内路径（/atlas-ui/）或完整地址；不填 = 不在应用切换里出现">
          <Input />
        </Form.Item>
        <Form.Item name="description" label="说明">
          <Input.TextArea rows={2} />
        </Form.Item>
      </Form>
    </Modal>
  );
}
