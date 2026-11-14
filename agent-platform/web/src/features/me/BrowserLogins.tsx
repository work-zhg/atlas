"use client";

/**
 * 我的登录（c4-model/agent/browser/code-browser-login.html）：智能体的浏览器里，你登录过哪些网站。
 *
 * ★ 只有站点和时间，没有内容：登录态加密存在平台的库里，这里看不到、也拿不出来。
 * ★ 删除只影响以后：库里删掉，正在跑的浏览器里也尽力清掉；智能体下次再用到要重新登录。
 */
import { Alert, App as AntdApp, Button, Empty, Table, Tag } from "antd";

import { type BrowserLogin, useBrowserLogins, useForgetLogin } from "@atlas/chat-ui/src/api/browser";
import { ApiError } from "@atlas/chat-ui/src/lib/http";

export function BrowserLogins() {
  const { modal, message } = AntdApp.useApp();
  const logins = useBrowserLogins();
  const forget = useForgetLogin();

  if (logins.error instanceof ApiError && logins.error.kind === "browser_disabled") {
    return <Alert type="info" showIcon message="这个部署没有开浏览器能力" />;
  }

  const remove = (row: BrowserLogin) =>
    modal.confirm({
      title: `删除 ${row.site} 的登录？`,
      content: "智能体以后再访问这个网站要重新登录。正在运行的浏览器里也会清掉。",
      okText: "删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: () =>
        forget.mutateAsync(row.site).then(
          () => message.success(`已删除 ${row.site} 的登录`),
          (e: Error) => message.error(`没有删除成功：${e.message}`),
        ),
    });

  return (
    <div style={{ maxWidth: 760 }}>
      <p style={{ margin: "0 0 10px", color: "var(--fg-2)" }}>
        你在接管卡片里登录过的网站，平台加密保管登录态，智能体下次换了浏览器也不用再登录。
        这里只有站点和时间，看不到内容。
      </p>
      {logins.error && !(logins.error instanceof ApiError && logins.error.kind === "browser_disabled") && (
        <Alert type="error" showIcon style={{ marginBottom: 12 }} message={logins.error.message} />
      )}
      <Table<BrowserLogin>
        rowKey="site"
        size="small"
        loading={logins.isLoading}
        dataSource={logins.data ?? []}
        pagination={false}
        locale={{ emptyText: <Empty description="还没有登录过的网站" /> }}
        columns={[
          {
            title: "网站",
            dataIndex: "site",
            render: (site: string, row) => (
              <span>
                {site}
                {row.expired && (
                  <Tag style={{ marginLeft: 8 }} color="default">
                    已过期
                  </Tag>
                )}
              </span>
            ),
          },
          { title: "最近保存", dataIndex: "captured_at", width: 180, render: when },
          {
            title: "过期时间",
            dataIndex: "expires_at",
            width: 180,
            render: (v: string | null | undefined) => (v ? when(v) : "关掉浏览器前一直有效"),
          },
          {
            title: "",
            width: 80,
            render: (_: unknown, row) => (
              <Button size="small" type="text" danger onClick={() => remove(row)}>
                删除
              </Button>
            ),
          },
        ]}
      />
    </div>
  );
}

function when(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("zh-CN", { hour12: false });
}
