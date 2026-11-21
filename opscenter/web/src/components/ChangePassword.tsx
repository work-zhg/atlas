"use client";

/** 右上角账号菜单 › 修改密码。改了之后别处的登录作废，这一个保留。 */
import { App, Form, Input, Modal } from "antd";
import { useState } from "react";

import { PasswordRules, passwordOk } from "@/components/auth/AuthCard";
import { ErrorBox } from "@/components/common";
import { api } from "@/lib/api";

export function ChangePassword({ onClose }: { onClose: () => void }) {
  const { message } = App.useApp();
  const [form] = Form.useForm<{ old_password: string; new_password: string; again: string }>();
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const pw = Form.useWatch("new_password", form) ?? "";
  const old = Form.useWatch("old_password", form) ?? "";

  async function submit(v: { old_password: string; new_password: string }) {
    setBusy(true);
    setError(null);
    try {
      await api.post("/auth/password", { old_password: v.old_password, new_password: v.new_password });
      message.success("密码已修改，其他地方的登录已退出");
      onClose();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open title="修改密码" onCancel={onClose} onOk={() => form.submit()} okText="保存" confirmLoading={busy} okButtonProps={{ disabled: !passwordOk(pw, old) }}>
      <ErrorBox error={error} />
      <Form form={form} layout="vertical" onFinish={submit} requiredMark={false}>
        <Form.Item name="old_password" label="当前密码" rules={[{ required: true, message: "请输入当前密码" }]}>
          <Input.Password autoComplete="current-password" />
        </Form.Item>
        <Form.Item name="new_password" label="新密码" extra={<PasswordRules value={pw} old={old} />} rules={[{ required: true, message: "请输入新密码" }]}>
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item
          name="again"
          label="再输一次"
          dependencies={["new_password"]}
          rules={[
            { required: true, message: "请再输一次" },
            ({ getFieldValue }) => ({
              validator: (_, v) => (!v || v === getFieldValue("new_password") ? Promise.resolve() : Promise.reject(new Error("两次输入不一样"))),
            }),
          ]}
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
      </Form>
    </Modal>
  );
}
