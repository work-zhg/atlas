"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { App, Form, Input, Modal } from "antd";
import { useEffect } from "react";

import { api, errorText } from "@/lib/api";
import type { User } from "@/lib/types";

import { DeptSelect, showOnce } from "./common";

/** 新建 / 编辑用户。★ 没有岗位、直属上级字段：岗位在权限管理中授予，直属上级由组织计算。 */
export function UserForm({
  open,
  user,
  presetDept,
  onClose,
}: {
  open: boolean;
  user?: User | null;
  presetDept?: string;
  onClose: () => void;
}) {
  const [form] = Form.useForm();
  const qc = useQueryClient();
  const { message, modal } = App.useApp();

  useEffect(() => {
    if (!open) return;
    form.resetFields();
    form.setFieldsValue(
      user
        ? { name: user.name, account: user.account, email: user.email, phone: user.phone ?? "", dept_id: user.dept_id }
        : { dept_id: presetDept },
    );
  }, [open, user, presetDept, form]);

  const save = useMutation({
    mutationFn: async (v: Record<string, string>) => {
      if (user) {
        const r = await api.patch<{ leaders_cleared: string[] }>(`/users/${user.id}`, {
          name: v.name,
          email: v.email,
          phone: v.phone || null,
          dept_id: v.dept_id,
          version: user.version,
        });
        return { cleared: r.leaders_cleared, temp: null as string | null, account: user.account, name: v.name };
      }
      const r = await api.post<{ user: User; temp_password: string }>("/users", { ...v, phone: v.phone || null });
      return { cleared: [] as string[], temp: r.temp_password, account: r.user.account, name: r.user.name };
    },
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["users"] });
      qc.invalidateQueries({ queryKey: ["user"] });
      qc.invalidateQueries({ queryKey: ["org"] });
      onClose();
      if (r.temp) {
        showOnce(modal, "用户已创建", [["用户", `${r.name}（${r.account}）`], ["临时密码", r.temp]], "临时密码只显示这一次，请通过安全渠道告知用户；首次登录必须修改密码。");
      } else if (r.cleared.length) {
        message.warning(`已保存；已自动清空「${r.cleared.join("」「")}」的负责人`);
      } else {
        message.success("已保存");
      }
    },
    onError: (e) => message.error(errorText(e)),
  });

  return (
    <Modal
      open={open}
      title={user ? `编辑用户 · ${user.name}` : "新建用户"}
      onCancel={onClose}
      onOk={() => form.submit()}
      confirmLoading={save.isPending}
      okText="保存"
      width={560}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" onFinish={(v) => save.mutate(v)} requiredMark>
        <Form.Item name="name" label="姓名" rules={[{ required: true, message: "请填写姓名" }]}>
          <Input maxLength={32} />
        </Form.Item>
        <Form.Item
          name="account"
          label="账号"
          extra={user ? "账号创建后不可修改（各系统以它识别用户）" : "小写字母、数字、点、下划线，3–32 位；创建后不可修改"}
          rules={[{ required: true, pattern: /^[a-z0-9._]{3,32}$/, message: "账号格式不正确" }]}
        >
          <Input disabled={!!user} />
        </Form.Item>
        <Form.Item name="email" label="邮箱" rules={[{ required: true, type: "email", message: "邮箱格式不正确" }]}>
          <Input />
        </Form.Item>
        <Form.Item name="phone" label="手机" rules={[{ pattern: /^(1\d{10})?$/, message: "11 位手机号" }]}>
          <Input />
        </Form.Item>
        <Form.Item name="dept_id" label="部门" rules={[{ required: true, message: "请选择部门" }]}>
          <DeptSelect />
        </Form.Item>
        {!user && <div className="uc-muted">创建后生成一次性临时密码，账号为「未激活」；用户首次登录时必须修改密码。</div>}
      </Form>
    </Modal>
  );
}
