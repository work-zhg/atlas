"use client";

/**
 * 沙箱模板（运行环境）：决定一个沙箱 Pod 能拿到多大权限，所有租户共用、只能由运营中心改。
 * 修订只是存一个新版本，发布才推到 OpenSandbox；被人在集群里手工改过会标「漂移」。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Drawer, Form, Input, Space, Table, Tag, Typography } from "antd";
import { useState } from "react";

import { ErrorBox, PageHead, errorText } from "@/components/common";
import { sys } from "@/lib/api";
import { dateTime } from "@/lib/format";
import { useMe, useSystems, withCapability } from "@/lib/session";
import type { SandboxTemplate } from "@/lib/types";

const MONO = { fontFamily: "Menlo, Consolas, monospace", fontSize: 12.5 };

export default function TemplatesPage() {
  const { data: systems } = useSystems();
  const env = withCapability(systems, "runtime_env")[0];
  if (!env) return <PageHead title="沙箱模板" sub="没有登记提供运行环境的系统" />;
  return <Templates code={env.code} />;
}

function Templates({ code }: { code: string }) {
  const ops = sys(code);
  const { data: me } = useMe();
  const admin = me?.role === "ADMIN";
  const q = useQuery({ queryKey: ["templates", code], queryFn: () => ops.get<SandboxTemplate[]>("/sandbox-templates") });
  const [open, setOpen] = useState<string | "new" | null>(null);
  return (
    <>
      <PageHead
        title="沙箱模板"
        sub="运行环境：所有租户的沙箱都按这些模板建。修订存新版本，发布后新建的沙箱才用它"
        extra={
          admin && (
            <Button type="primary" onClick={() => setOpen("new")}>
              新建模板
            </Button>
          )
        }
      />
      <ErrorBox error={q.error} />
      <Table<SandboxTemplate>
        rowKey="code"
        loading={q.isLoading}
        dataSource={q.data ?? []}
        pagination={false}
        columns={[
          {
            title: "编码",
            dataIndex: "code",
            render: (v: string) => (
              <Button type="link" style={{ padding: 0 }} onClick={() => setOpen(v)}>
                {v}
              </Button>
            ),
          },
          { title: "说明", dataIndex: "description" },
          { title: "OpenSandbox", dataIndex: "server" },
          {
            title: "版本",
            render: (_, t) => (
              <Space size={4}>
                <span>当前 v{t.version}</span>
                {t.published_version === null ? (
                  <Tag>没发布过</Tag>
                ) : t.published_version === t.version ? (
                  <Tag color="green">已发布</Tag>
                ) : (
                  <Tag color="orange">已发布 v{t.published_version}，有新修订</Tag>
                )}
              </Space>
            ),
          },
          { title: "发布时间", dataIndex: "published_at", render: dateTime },
        ]}
      />
      {open && <TemplateDrawer system={code} code={open} admin={admin} onClose={() => setOpen(null)} />}
    </>
  );
}

function TemplateDrawer({
  system,
  code,
  admin,
  onClose,
}: {
  system: string;
  code: string | "new";
  admin: boolean;
  onClose: () => void;
}) {
  const ops = sys(system);
  const qc = useQueryClient();
  const { message } = App.useApp();
  const isNew = code === "new";
  const q = useQuery({
    queryKey: ["template", system, code],
    enabled: !isNew,
    queryFn: () => ops.get<SandboxTemplate>(`/sandbox-templates/${code}`),
  });
  const [form] = Form.useForm();
  const [busy, setBusy] = useState(false);
  const t = q.data;

  async function act(fn: () => Promise<unknown>, ok: string) {
    setBusy(true);
    try {
      await fn();
      message.success(ok);
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["templates", system] }),
        qc.invalidateQueries({ queryKey: ["template", system, code] }),
      ]);
      return true;
    } catch (e) {
      message.error(errorText(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  return (
    <Drawer open width={760} title={isNew ? "新建沙箱模板" : `沙箱模板 ${code}`} onClose={onClose}>
      <ErrorBox error={q.error} />
      {!isNew && t && (
        <Space style={{ marginBottom: 12 }} wrap>
          <Tag>当前 v{t.version}</Tag>
          {t.published_version !== null && <Tag color="green">已发布 v{t.published_version}</Tag>}
          {t.drifted && <Tag color="red">集群里的和已发布的对不上（被手工改过）</Tag>}
          {admin && (
            <Button
              loading={busy}
              disabled={t.published_version === t.version && !t.drifted}
              onClick={() => act(() => ops.post(`/sandbox-templates/${code}/publish`), "已发布")}
            >
              发布 v{t.version}
            </Button>
          )}
        </Space>
      )}
      {(isNew || t) && (
        <Form
          key={t?.version ?? "new"}
          form={form}
          layout="vertical"
          disabled={!admin}
          initialValues={isNew ? { server: "opensandbox-sbx" } : { description: t?.description, yaml: t?.yaml }}
          onFinish={async (v) => {
            const ok = isNew
              ? await act(() => ops.post("/sandbox-templates", v), "已新建")
              : await act(() => ops.put(`/sandbox-templates/${code}`, { yaml: v.yaml, description: v.description }), "已存为新修订");
            if (ok && isNew) onClose();
          }}
        >
          {isNew && (
            <>
              <Form.Item name="code" label="编码" rules={[{ required: true }]}>
                <Input />
              </Form.Item>
              <Form.Item name="server" label="OpenSandbox" rules={[{ required: true }]}>
                <Input />
              </Form.Item>
            </>
          )}
          <Form.Item name="description" label="说明">
            <Input />
          </Form.Item>
          <Form.Item name="yaml" label="模板（YAML）" rules={[{ required: true }]}>
            <Input.TextArea rows={22} style={MONO} spellCheck={false} />
          </Form.Item>
          {admin && (
            <Button type="primary" htmlType="submit" loading={busy}>
              {isNew ? "新建" : "存为新修订"}
            </Button>
          )}
          <Typography.Paragraph type="secondary" style={{ marginTop: 12 }}>
            存修订不影响正在用的沙箱；发布后新建的沙箱才按它建。
          </Typography.Paragraph>
        </Form>
      )}
    </Drawer>
  );
}
