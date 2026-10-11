"use client";

/**
 * 我的 Gitee 账号（产物仓库与 Agent 提交 · §7）：
 * 绑定只为证明账号归属（OAuth，TeamFlow 不保存任何令牌）；绑定后按团队身份成为各项目产物仓库的协作者，
 * 本地用自己的 Gitee 凭据 clone / pull。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Card, Descriptions, Popconfirm, Space, Table, Tag } from "antd";
import Link from "next/link";
import { useState } from "react";

import { PageHead } from "@/components/common";
import { api, errorText } from "@/lib/api";
import { dt } from "@/lib/format";
import type { GitAccountView } from "@/lib/types";

export default function AccountPage() {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [busy, setBusy] = useState(false);
  const { data: v, error } = useQuery({
    queryKey: ["git-account"],
    queryFn: () => api.get<GitAccountView>("/me/git-account"),
  });

  async function bind() {
    setBusy(true);
    try {
      const { url } = await api.post<{ url: string }>("/me/git-account/oauth");
      window.location.href = url; // 去 Gitee 授权，回到 /account/gitee
    } catch (e) {
      message.error(errorText(e));
      setBusy(false);
    }
  }
  async function unbind() {
    try {
      qc.setQueryData(["git-account"], await api.del<GitAccountView>("/me/git-account"));
      message.success("已解绑，并已从各项目仓库移除");
    } catch (e) {
      message.error(errorText(e));
    }
  }

  return (
    <>
      <PageHead title="Gitee 账号" sub="绑定后按团队身份获得项目产物仓库的访问权限，在本地用自己的 Gitee 账号 clone / pull" />
      {error && <Alert type="error" showIcon message={errorText(error)} />}
      {v && (
        <Space direction="vertical" size={16} style={{ width: "100%", maxWidth: 820 }}>
          <Card size="small" title="绑定">
            {v.account ? (
              <Space direction="vertical">
                <Descriptions
                  size="small"
                  column={1}
                  items={[
                    { label: "Gitee 账号", children: <b>{v.account.login}</b> },
                    { label: "名称", children: v.account.name ?? "—" },
                    { label: "绑定时间", children: dt(v.account.bound_at) },
                  ]}
                />
                <Space>
                  <Button loading={busy} disabled={!v.oauth_enabled} onClick={bind}>
                    重新绑定
                  </Button>
                  <Popconfirm title="解绑后将从所有项目产物仓库中移除你的访问权限。确认解绑？" onConfirm={unbind}>
                    <Button danger>解绑</Button>
                  </Popconfirm>
                </Space>
              </Space>
            ) : v.oauth_enabled ? (
              <Space direction="vertical">
                <span className="tf-muted">还没有绑定。授权只读取你的 Gitee 用户名，TeamFlow 不保存任何令牌。</span>
                <Button type="primary" loading={busy} onClick={bind}>
                  绑定 Gitee 账号
                </Button>
              </Space>
            ) : (
              <Alert type="info" showIcon message="平台还没有配置 Gitee OAuth 应用，暂时不能绑定。请联系平台管理员。" />
            )}
          </Card>
          {v.account && (
            <Card size="small" title="我的仓库权限">
              <Table
                size="small"
                rowKey="project_id"
                pagination={false}
                dataSource={v.repositories}
                locale={{ emptyText: "你所在的团队还没有项目产物仓库" }}
                columns={[
                  { title: "项目", render: (_, r) => <Link href={`/projects/${r.project_id}/settings`}>查看仓库</Link> },
                  {
                    title: "权限",
                    render: (_, r) => (r.permission === "push" ? <Tag color="blue">可提交</Tag> : <Tag>只读</Tag>),
                  },
                  { title: "状态", render: (_, r) => (r.error ? <Tag color="red">{r.error}</Tag> : <Tag color="green">已同步</Tag>) },
                ]}
              />
            </Card>
          )}
        </Space>
      )}
    </>
  );
}
