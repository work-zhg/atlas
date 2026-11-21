"use client";

/**
 * 沙箱模板（运行环境）：决定一个沙箱 Pod 能拿到多大权限，所有企业共用、只能由运营中心改。
 * 修订只是存一个新版本，发布才推到 OpenSandbox；被人在集群里手工改过会标「被手工改过」，重新发布就覆盖回去。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Drawer, Empty, Form, Input, Space, Table, Tag, Tooltip } from "antd";
import { useState } from "react";

import { ErrorBox, OBSERVER_TIP, PageHead, errorText } from "@/components/common";
import { Icon } from "@/components/Icon";
import { sys } from "@/lib/api";
import { dateTime } from "@/lib/format";
import { useMe, useSystems, withCapability } from "@/lib/session";
import type { SandboxTemplate } from "@/lib/types";

export default function TemplatesPage() {
  const { data: systems } = useSystems();
  const env = withCapability(systems, "runtime_env")[0];
  if (systems && !env)
    return (
      <>
        <PageHead title="沙箱模板" />
        <Empty description="没有登记提供运行环境的系统" />
      </>
    );
  return env ? <Templates code={env.code} /> : null;
}

function VersionTags({ t }: { t: SandboxTemplate }) {
  return (
    <>
      {t.published_version === null ? (
        <Tag>没发布过</Tag>
      ) : t.published_version === t.version ? (
        <Tag color="success">已发布</Tag>
      ) : (
        <Tag color="warning">已发布 v{t.published_version}，有新修订</Tag>
      )}
      {t.drifted && (
        <Tag color="error" title="集群里的和已发布的对不上">
          被手工改过
        </Tag>
      )}
    </>
  );
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
        sub="智能体的沙箱按模板创建，所有企业共用。改了先存成新修订，发布后新建的沙箱才用它"
        extra={
          <Tooltip title={admin ? undefined : OBSERVER_TIP}>
            <Button type="primary" icon={<Icon name="plus" />} disabled={!admin} onClick={() => setOpen("new")}>
              新建模板
            </Button>
          </Tooltip>
        }
      />
      <ErrorBox error={q.error} />
      <Table<SandboxTemplate>
        rowKey="code"
        loading={q.isLoading}
        dataSource={q.data ?? []}
        pagination={false}
        onRow={(t) => ({ onClick: () => setOpen(t.code), className: "ops-row-click" })}
        columns={[
          {
            title: "模板",
            render: (_, t) => (
              <div>
                <b className="ops-mono">{t.code}</b>
                <div className="ops-sub">{t.description || "没有说明"}</div>
              </div>
            ),
          },
          { title: "OpenSandbox 实例", dataIndex: "server", render: (v: string) => <span className="ops-mono ops-muted">{v}</span> },
          {
            title: "版本",
            render: (_, t) => (
              <div className="ops-row" style={{ gap: 4, flexWrap: "wrap" }}>
                <span>v{t.version}</span>
                <VersionTags t={t} />
              </div>
            ),
          },
          { title: "发布时间", dataIndex: "published_at", render: (v: string | null) => <span className="ops-muted">{dateTime(v)}</span> },
          { title: "", width: 40, render: () => <Icon name="right" size={14} style={{ color: "var(--fg-3)" }} /> },
        ]}
      />
      {open && <TemplateDrawer system={code} code={open} admin={admin} onClose={() => setOpen(null)} />}
    </>
  );
}

function TemplateDrawer({ system, code, admin, onClose }: { system: string; code: string | "new"; admin: boolean; onClose: () => void }) {
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
  const canPublish = !!t && (t.published_version !== t.version || !!t.drifted);

  async function act(fn: () => Promise<unknown>, ok: string) {
    setBusy(true);
    try {
      await fn();
      message.success(ok);
      await Promise.all([qc.invalidateQueries({ queryKey: ["templates", system] }), qc.invalidateQueries({ queryKey: ["template", system, code] })]);
      return true;
    } catch (e) {
      message.error(errorText(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  const publish = () => act(() => ops.post(`/sandbox-templates/${code}/publish`), `已发布 v${t?.version}，已同步到集群`);

  return (
    <Drawer
      open
      width={780}
      title={
        <div className="ops-row">
          <span className={isNew ? "" : "ops-mono"}>{isNew ? "新建沙箱模板" : code}</span>
          {t && <VersionTags t={t} />}
        </div>
      }
      onClose={onClose}
      footer={
        admin && (
          <div className="ops-row">
            <span className="ops-sub ops-grow">{isNew ? "建好是 v1，发布后才生效" : "保存 = 存成新修订（不影响在用的）；发布后新建的沙箱才用它"}</span>
            <Button loading={busy} onClick={() => form.submit()}>
              {isNew ? "新建" : "存为新修订"}
            </Button>
            {!isNew && (
              <Button type="primary" loading={busy} disabled={!canPublish} onClick={() => void publish()}>
                发布 v{t?.version}
              </Button>
            )}
          </div>
        )
      }
    >
      <ErrorBox error={q.error} />
      {t?.drifted && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 14 }}
          message={`集群里的模板和已发布的 v${t.published_version} 对不上，被人手工改过。新建的沙箱按集群里的走`}
          action={
            admin && (
              <Button size="small" danger onClick={() => void publish()}>
                重新发布覆盖
              </Button>
            )
          }
        />
      )}
      {t && (
        <dl className="ops-kv" style={{ marginBottom: 16 }}>
          <dt>沙箱实例</dt>
          <dd className="ops-mono">{t.server}</dd>
          <dt>已发布</dt>
          <dd>{t.published_version !== null ? `v${t.published_version} · ${dateTime(t.published_at)}` : "没发布过"}</dd>
          <dt>最后修改</dt>
          <dd>
            v{t.version} · {t.updated_by}
          </dd>
        </dl>
      )}
      {(isNew || t) && (
        <Form
          key={t?.version ?? "new"}
          form={form}
          layout="vertical"
          disabled={!admin}
          requiredMark={false}
          initialValues={isNew ? { server: "opensandbox-sbx" } : { description: t?.description, yaml: t?.yaml }}
          onFinish={async (v) => {
            const ok = isNew
              ? await act(() => ops.post("/sandbox-templates", v), "已新建 v1，发布后才生效")
              : await act(() => ops.put(`/sandbox-templates/${code}`, { yaml: v.yaml, description: v.description }), "已存为新修订");
            if (ok && isNew) onClose();
          }}
        >
          {isNew && (
            <Space.Compact block style={{ gap: 14 }}>
              <Form.Item name="code" label="编码" style={{ flex: 1 }} rules={[{ required: true, message: "请输入编码" }]}>
                <Input className="ops-mono" placeholder="如 sbx-gpu" />
              </Form.Item>
              <Form.Item name="server" label="OpenSandbox 实例" style={{ flex: 1 }} rules={[{ required: true, message: "请输入实例" }]}>
                <Input className="ops-mono" />
              </Form.Item>
            </Space.Compact>
          )}
          <Form.Item name="description" label="说明">
            <Input />
          </Form.Item>
          <Form.Item name="yaml" label="模板（YAML）" rules={[{ required: true, message: "请填写模板" }]}>
            <Input.TextArea rows={22} className="ops-mono" spellCheck={false} style={{ background: "var(--bg-side)" }} />
          </Form.Item>
        </Form>
      )}
    </Drawer>
  );
}
