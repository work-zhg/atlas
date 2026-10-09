"use client";

import { SettingOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Alert, Breadcrumb, Button, Card, Space, Spin, Tag } from "antd";
import Link from "next/link";
import { use } from "react";

import { AgentAvatar, EmptyBox, PageHead, PersonAvatar, StatusTag } from "@/components/common";
import { ProcessList } from "@/components/ProcessList";
import { api, errorText } from "@/lib/api";
import type { ProjectDetail } from "@/lib/types";

/** 项目首页：以流程为主；提示条 + 角色概览。 */
export default function ProjectPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { data: p, error } = useQuery({ queryKey: ["project", id], queryFn: () => api.get<ProjectDetail>(`/projects/${id}`) });
  if (error) return <EmptyBox text={errorText(error)} />;
  if (!p) return <Spin />;

  return (
    <>
      <Breadcrumb
        items={[
          { title: <Link href="/teams">团队</Link> },
          { title: <Link href={`/teams/${p.team_id}`}>{p.team.name}</Link> },
          { title: p.name },
        ]}
        style={{ marginBottom: 12 }}
      />
      <PageHead
        title={
          <Space>
            📁 {p.name}
            {p.status === "archived" && <StatusTag status="archived" />}
          </Space>
        }
        sub={
          <>
            {p.description || "—"} · 🧭 {p.template_name ?? "未绑定模板"} {p.template_version}
          </>
        }
        extra={
          <Link href={`/projects/${p.id}/settings`}>
            <Button icon={<SettingOutlined />}>项目设置</Button>
          </Link>
        }
      />
      <Space direction="vertical" style={{ width: "100%" }} size={12}>
        {p.hints.map((h) => (
          <Alert
            key={h.kind}
            type={h.kind === "archived" ? "info" : "warning"}
            showIcon
            message={h.message}
            action={
              h.kind !== "archived" && (
                <Link href={`/projects/${p.id}/settings`}>
                  <Button size="small">去配置</Button>
                </Link>
              )
            }
          />
        ))}
        <ProcessList project={p} />
        <Card title="角色分配" size="small">
          <Space direction="vertical" style={{ width: "100%" }}>
            {p.roles.roles.map((r) => (
              <div key={r.name} style={{ display: "flex", gap: 12, alignItems: "center" }}>
                <Tag color={r.kind === "exec" ? "blue" : "purple"} style={{ width: 120, textAlign: "center" }}>
                  {r.kind === "exec" ? "⚙️" : "✅"} {r.name}
                </Tag>
                <Space size={4}>
                  {r.users.map((u) => (
                    <Space key={u.id} size={4}>
                      <PersonAvatar name={u.name} size={22} />
                      <span style={{ fontSize: 13 }}>{u.name}</span>
                    </Space>
                  ))}
                  {r.agents.map((a) => (
                    <Space key={a.id} size={4}>
                      <AgentAvatar name={a.name} available={a.available} size={22} />
                      <span style={{ fontSize: 13 }}>{a.name}</span>
                    </Space>
                  ))}
                </Space>
                {r.problems.map((x) => (
                  <Tag key={x} color="red">
                    {x}
                  </Tag>
                ))}
              </div>
            ))}
          </Space>
        </Card>
      </Space>
    </>
  );
}
