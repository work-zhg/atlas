"use client";

/**
 * 个人中心 › 账号安全：修改密码（弹窗，规则逐条打勾；改完其他设备上的登录会退出）；
 * 最近登录（失败的也列出，便于本人发现异常）。
 */
import { useQuery } from "@tanstack/react-query";
import { App, Button, Card, Modal, Table, Tag } from "antd";
import { useState } from "react";

import { SetPasswordForm } from "@/components/auth/AuthCard";
import { PageHead, SquareIcon } from "@/components/common";
import { api } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { useMe } from "@/lib/session";
import type { LoginLog } from "@/lib/types";

export default function SecurityPage() {
  const { data: me } = useMe();
  const { message } = App.useApp();
  const [open, setOpen] = useState(false);
  const logs = useQuery({ queryKey: ["me", "login-logs"], queryFn: () => api.get<LoginLog[]>("/me/login-logs", { size: 50 }) });
  if (!me) return <Card loading />;
  return (
    <div style={{ maxWidth: 880 }}>
      <PageHead title="账号安全" />
      <Card>
        <div className="uc-row" style={{ gap: 14 }}>
          <SquareIcon icon="key" size={40} />
          <div className="uc-grow">
            <b>登录密码</b>
            <div className="uc-sub">要求：{me.password_policy}。修改后，其他设备上的登录会退出。</div>
          </div>
          <Button onClick={() => setOpen(true)}>修改密码</Button>
        </div>
      </Card>
      <Card
        style={{ marginTop: 14 }}
        title={
          <span>
            最近登录 <span className="uc-sub">最近 90 天</span>
          </span>
        }
        extra={<span className="uc-sub">不是你本人操作？立即修改密码</span>}
        styles={{ body: { padding: 0 } }}
      >
        <Table<LoginLog>
          rowKey={(r) => `${r.time}-${r.ip}-${r.ok}`}
          loading={logs.isLoading}
          dataSource={logs.data ?? []}
          pagination={false}
          locale={{ emptyText: "最近 90 天没有登录记录" }}
          columns={[
            { title: "时间", render: (_, r) => <span className="uc-nw">{fmtTime(r.time)}</span> },
            { title: "经由", render: (_, r) => <span className="uc-muted">{r.app ?? "统一登录"}</span> },
            { title: "IP", render: (_, r) => <span className="uc-mono">{r.ip ?? "—"}</span> },
            { title: "结果", render: (_, r) => (r.ok ? <Tag color="success">成功</Tag> : <Tag color="error">失败 · {r.reason}</Tag>) },
          ]}
        />
      </Card>
      <Modal open={open} footer={null} onCancel={() => setOpen(false)} destroyOnHidden width={440}>
        <div style={{ paddingTop: 8 }}>
          <SetPasswordForm
            me={me}
            submitText="修改密码"
            title="修改密码"
            onDone={() => {
              setOpen(false);
              message.success("密码已修改；其他设备上的登录已退出");
            }}
          />
        </div>
      </Modal>
    </div>
  );
}
