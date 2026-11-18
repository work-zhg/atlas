"use client";

/**
 * 个人中心 › 个人资料：姓名、部门由管理员维护（只读）；邮箱、手机本人可改。
 * 直属上级按组织架构计算，直接显示。
 */
import { useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, Descriptions, Divider, Form, Input } from "antd";

import { PageHead, PersonAvatar } from "@/components/common";
import { api, errorText } from "@/lib/api";
import { useMe } from "@/lib/session";

export default function ProfilePage() {
  const { data: me } = useMe();
  const qc = useQueryClient();
  const { message } = App.useApp();
  if (!me) return <Card loading />;

  async function save(v: { email: string; phone: string }) {
    try {
      await api.patch("/me", { email: v.email, phone: v.phone || null });
      qc.invalidateQueries({ queryKey: ["me"] });
      message.success("已保存");
    } catch (e) {
      message.error(errorText(e));
    }
  }

  return (
    <div style={{ maxWidth: 820 }}>
      <PageHead title="个人资料" />
      <Card>
        <div className="uc-row" style={{ gap: 14 }}>
          <PersonAvatar name={me.user.name} size={56} />
          <div className="uc-grow">
            <span style={{ fontSize: 18, fontWeight: 650 }}>{me.user.name}</span>
            <div className="uc-muted">
              {me.user.account} · {me.tenant.name}
            </div>
          </div>
        </div>
        <Divider />
        <Descriptions column={1} size="small" styles={{ label: { width: 96 } }}>
          <Descriptions.Item label="部门">{me.user.dept_path.join(" / ")}</Descriptions.Item>
          <Descriptions.Item label="直属上级">
            {me.manager ? (
              <span className="uc-row">
                <PersonAvatar name={me.manager.name} size={22} />
                {me.manager.name}
                <span className="uc-muted">（{me.manager.source === "own_dept" ? "所在部门负责人" : "上级部门负责人"}）</span>
              </span>
            ) : (
              "—"
            )}
          </Descriptions.Item>
        </Descriptions>
        <div className="uc-sub">姓名、部门由管理员维护；有误请联系管理员。</div>
        <Divider />
        <Form layout="vertical" initialValues={{ email: me.user.email, phone: me.user.phone ?? "" }} onFinish={save}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
            <Form.Item name="email" label="邮箱" rules={[{ required: true, type: "email", message: "邮箱格式不正确" }]}>
              <Input />
            </Form.Item>
            <Form.Item name="phone" label="手机" rules={[{ pattern: /^(1\d{10})?$/, message: "11 位手机号" }]}>
              <Input />
            </Form.Item>
          </div>
          <div style={{ textAlign: "right" }}>
            <Button type="primary" htmlType="submit">
              保存
            </Button>
          </div>
        </Form>
      </Card>
    </div>
  );
}
