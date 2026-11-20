"use client";

/**
 * 应用 › 已开通的应用：本租户开通了哪些应用（由运营中心开通），以及各自的可访问范围。
 * 接入应用、应用凭据、操作码、菜单、数据编码都由运营中心维护（多租户设计 §3.3），这里只读。
 * 卡片同 Atlas 的智能体卡片；左侧菜单直接列出各应用（见 AppShell）。
 */
import { useQuery } from "@tanstack/react-query";
import { Input, Segmented, Tag } from "antd";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { AppIcon, PageHead } from "@/components/common";
import { Icon } from "@/components/Icon";
import { api } from "@/lib/api";
import type { AppInfo } from "@/lib/types";

export default function AppsPage() {
  const router = useRouter();
  const [status, setStatus] = useState("all");
  const [q, setQ] = useState("");
  const list = useQuery({ queryKey: ["apps"], queryFn: () => api.get<AppInfo[]>("/apps") });
  const kw = q.trim().toLowerCase();
  const apps = (list.data ?? []).filter(
    (a) => (status === "all" || a.status === status) && (!kw || a.name.toLowerCase().includes(kw) || (a.code ?? "").includes(kw)),
  );

  const scopeText = (a: AppInfo) =>
    a.status === "disabled" ? "已停用" : a.scope_all ? "全员" : a.scope_dept_ids?.length ? `指定部门 · ${a.accessible_users ?? 0} 人` : "无人可访问";

  return (
    <>
      <PageHead title="已开通的应用" sub="本租户开通的应用（由运营中心开通）。在应用里设置本租户哪些人能用。" />
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
    </>
  );
}
