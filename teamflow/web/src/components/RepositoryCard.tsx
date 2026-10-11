"use client";

/**
 * 项目产物仓库（产物仓库与 Agent 提交 · §7 §9）。
 * ★ 产物由 Agent 在会话 Pod 里用 git 提交；成员绑定 Gitee 账号后按团队身份成为协作者，在本地 clone / pull。
 * ★ 团队管理员可开启「允许管理员直接提交」（push）；默认所有人只读。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Card, Space, Switch, Table, Tag, Typography } from "antd";
import Link from "next/link";
import { useState } from "react";

import { api, errorText } from "@/lib/api";
import { dt } from "@/lib/format";
import type { ProjectDetail, ProjectRepository } from "@/lib/types";

const PERM = { pull: "只读", push: "可提交" } as const;

export function RepositoryCard({ project, editable }: { project: ProjectDetail; editable: boolean }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [busy, setBusy] = useState(false);
  const key = ["project-repository", project.id];
  const { data: r, error } = useQuery({
    queryKey: key,
    queryFn: () => api.get<ProjectRepository>(`/projects/${project.id}/repository`),
  });

  async function run(fn: () => Promise<unknown>, ok: string) {
    setBusy(true);
    try {
      await fn();
      await Promise.all([qc.invalidateQueries({ queryKey: key }), qc.invalidateQueries({ queryKey: ["project", project.id] })]);
      message.success(ok);
    } catch (e) {
      message.error(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card
      title="产物仓库"
      size="small"
      extra={
        r?.can_sync && (
          <Button size="small" loading={busy} onClick={() => run(() => api.post(`/projects/${project.id}/repository/sync`), "已同步")}>
            {r.status === "ready" ? "同步协作者" : "重试创建仓库"}
          </Button>
        )
      }
    >
      {error && <Alert type="error" showIcon message={errorText(error)} />}
      {r && (
        <Space direction="vertical" size={10} style={{ width: "100%" }}>
          <div className="tf-muted" style={{ fontSize: 12.5 }}>
            流程产物由 Agent 在会话里用 git 提交到这里；TeamFlow 观察提交、登记版本。成员绑定 Gitee 账号后可在本地查看，
            {r.allow_admin_commit ? "团队管理员可直接提交修订。" : "默认只读。"}
          </div>
          {r.status === "error" && <Alert type="warning" showIcon message={`仓库未就绪：${r.error ?? "未知原因"}`} />}
          {r.status === "none" && <Alert type="info" showIcon message="仓库还没有创建（发起流程前会自动创建）" />}
          {r.full_name && (
            <>
              <div>
                <a href={r.web_url!} target="_blank" rel="noreferrer">
                  {r.full_name} ↗
                </a>
              </div>
              <Typography.Text copyable className="tf-mono" style={{ fontSize: 12.5 }}>
                {`git clone ${r.clone_url}`}
              </Typography.Text>
            </>
          )}
          <div style={{ fontSize: 13 }}>
            我的访问：
            {r.me.login ? (
              <>
                Gitee 账号 <b>{r.me.login}</b>{" "}
                {r.me.permission ? <Tag color={r.me.permission === "push" ? "blue" : undefined}>{PERM[r.me.permission]}</Tag> : <Tag>无</Tag>}
                {r.me.error && <Tag color="red">同步失败：{r.me.error}</Tag>}
              </>
            ) : (
              <>
                未绑定 Gitee 账号，<Link href="/account">去绑定</Link>
              </>
            )}
          </div>
          {r.can_sync && (
            <Space>
              <Switch
                checked={r.allow_admin_commit}
                disabled={!editable || busy}
                onChange={(on) =>
                  run(() => api.patch(`/projects/${project.id}`, { allow_admin_commit: on, version: project.version }), "已保存")
                }
              />
              <span style={{ fontSize: 13 }}>允许团队管理员用自己的 Gitee 账号直接提交</span>
            </Space>
          )}
          {r.collaborators && (
            <Table
              size="small"
              rowKey="login"
              pagination={false}
              dataSource={r.collaborators}
              locale={{ emptyText: "还没有成员绑定 Gitee 账号" }}
              columns={[
                { title: "成员", render: (_, c) => c.user_name ?? "—" },
                { title: "Gitee 账号", dataIndex: "login" },
                { title: "权限", render: (_, c) => PERM[c.permission] },
                {
                  title: "同步",
                  render: (_, c) => (c.error ? <Tag color="red">{c.error}</Tag> : c.synced_at ? dt(c.synced_at) : <Tag>待同步</Tag>),
                },
              ]}
            />
          )}
        </Space>
      )}
    </Card>
  );
}
