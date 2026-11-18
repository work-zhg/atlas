"use client";

/**
 * 应用 › 应用目录：接入的系统在服务端调用开放接口，获取用户、组织与本应用的权限。
 * 卡片同 Atlas 的智能体卡片；左侧菜单直接列出各应用（见 AppShell）。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Form, Input, Modal, Segmented, Tag } from "antd";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { AppIcon, PageHead, showOnce } from "@/components/common";
import { Icon } from "@/components/Icon";
import { api, errorText } from "@/lib/api";
import { HOME_URL_RULE } from "@/lib/format";
import { useCan } from "@/lib/session";
import type { AppInfo } from "@/lib/types";

export default function AppsPage() {
  const can = useCan();
  const router = useRouter();
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState("all");
  const [q, setQ] = useState("");
  const [form] = Form.useForm();
  const list = useQuery({ queryKey: ["apps"], queryFn: () => api.get<AppInfo[]>("/apps") });
  const kw = q.trim().toLowerCase();
  const apps = (list.data ?? []).filter(
    (a) => (status === "all" || a.status === status) && (!kw || a.name.toLowerCase().includes(kw) || (a.code ?? "").includes(kw)),
  );

  async function create(v: { name: string; code?: string; description?: string; icon?: string; home_url?: string }) {
    try {
      const r = await api.post<{ app: AppInfo; app_secret: string }>("/apps", v);
      qc.invalidateQueries({ queryKey: ["apps"] });
      setOpen(false);
      showOnce(
        modal,
        `应用已接入 · ${r.app.name}`,
        [
          ...(r.app.code ? ([["应用编码", r.app.code]] as [string, string][]) : []),
          ["App Key", r.app.app_key ?? ""],
          ["App Secret", r.app_secret],
        ],
        "App Secret 只显示这一次，只能保存在应用后端；新应用默认无人可访问，请在「接入配置」里设置可访问范围。",
      );
      router.push(`/apps/${r.app.id}`);
    } catch (e) {
      message.error(errorText(e));
    }
  }

  const scopeText = (a: AppInfo) =>
    a.status === "disabled" ? "已停用" : a.scope_all ? "全员" : a.scope_dept_ids?.length ? `指定部门 · ${a.accessible_users ?? 0} 人` : "无人可访问";

  return (
    <>
      <PageHead
        title="应用目录"
        sub="接入的系统在服务端调用开放接口，获取用户、组织与本应用的权限。"
        extra={
          can("app:manage") && (
            <Button type="primary" icon={<Icon name="plus" size={14} />} onClick={() => (form.resetFields(), setOpen(true))}>
              接入应用
            </Button>
          )
        }
      />
      <div className="uc-toolbar">
        <Segmented
          value={status}
          onChange={(v) => setStatus(String(v))}
          options={[
            { value: "all", label: "全部" },
            { value: "active", label: "已启用" },
            { value: "disabled", label: "已停用" },
          ]}
        />
        <Input allowClear prefix={<Icon name="search" size={14} style={{ color: "var(--fg-4)" }} />} placeholder="搜索应用名称或编码" style={{ width: 260 }} value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <div className="uc-grid">
        {apps.map((a) => (
          <button key={a.id} type="button" className="uc-acard" onClick={() => router.push(`/apps/${a.id}`)}>
            <div className="uc-row" style={{ gap: 10, width: "100%" }}>
              <AppIcon app={a} size={40} />
              <div className="uc-grow">
                <div className="uc-row" style={{ gap: 6 }}>
                  <b>{a.name}</b>
                  {a.is_builtin ? <Tag>内置</Tag> : a.status === "disabled" && <Tag>已停用</Tag>}
                </div>
                <div className="uc-sub uc-mono">{a.is_builtin ? "内置应用" : (a.code ?? "未设置编码（只能用 App Key 调用）")}</div>
              </div>
            </div>
            <div className="desc">{a.description || "—"}</div>
            <div className="foot" style={{ width: "100%" }}>
              <Icon name="users" size={14} />
              <span>{scopeText(a)}</span>
              <div className="uc-grow" />
              <span>
                {a.roles} 个角色 · {a.operations} 个操作
              </span>
            </div>
          </button>
        ))}
      </div>
      <Modal open={open} title="接入应用" onCancel={() => setOpen(false)} onOk={() => form.submit()} okText="创建" width={560}>
        <Form form={form} layout="vertical" onFinish={create}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <Form.Item name="name" label="应用名称" rules={[{ required: true }]}>
              <Input maxLength={50} placeholder="例如：报销系统" />
            </Form.Item>
            <Form.Item
              name="code"
              label="应用编码"
              extra="内网调用时请求头 X-Caller-App 写它"
              rules={[{ pattern: /^[a-z][a-z0-9-]{1,31}$/, message: "小写字母开头，小写字母、数字、短横线，2–32 位" }]}
            >
              <Input maxLength={32} placeholder="例如：expense" />
            </Form.Item>
          </div>
          <Form.Item name="home_url" label="入口地址" extra="「应用切换」「我的应用」点开去这里；不填则不提供入口" rules={[HOME_URL_RULE]}>
            <Input maxLength={500} placeholder="例如：/expense 或 https://expense.example.com" />
          </Form.Item>
          <Form.Item name="description" label="说明">
            <Input maxLength={200} placeholder="一句话说明这个应用做什么" />
          </Form.Item>
          <Form.Item name="icon" label="图标（emoji）">
            <Input maxLength={4} placeholder="🧱" style={{ width: 100 }} />
          </Form.Item>
          <div className="uc-sub">创建后生成 App Key / Secret；可访问范围默认「无人可访问」，在「接入配置」里设置。</div>
        </Form>
      </Modal>
    </>
  );
}
