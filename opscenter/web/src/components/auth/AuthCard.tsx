"use client";

/**
 * 运营中心的登录外壳：品牌 + 居中卡片（与用户中心的统一登录同一个样子）。登录页、设置新密码都用它。
 *
 * ★ 运营账号和各企业的账号是分开的：品牌写「运营中心」，副标题点明这是平台运营专用。
 */
import { useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Form, Input } from "antd";
import { useState, type ReactNode } from "react";

import { errorText } from "@/components/common";
import { Icon } from "@/components/Icon";
import { ApiError, api } from "@/lib/api";
import type { Me } from "@/lib/types";

import styles from "./auth.module.css";

export function AuthCard({ children }: { children: ReactNode }) {
  return (
    <div className={styles.wrap}>
      <div className={styles.brand}>
        <span className={styles.logo}>A</span>运营中心
      </div>
      <div className={styles.card}>{children}</div>
      <div className={styles.foot}>会话闲置 30 分钟自动退出 · 登录、操作都会记进运营审计</div>
    </div>
  );
}

/** 锁定、停用这类「要找人处理」的用警告色 */
export function AuthError({ error }: { error: unknown }) {
  const warn = error instanceof ApiError && error.code === "ACCOUNT_LOCKED";
  return <Alert type={warn ? "warning" : "error"} showIcon message={errorText(error)} style={{ marginBottom: 16 }} />;
}

/** 与后端 security.check_policy 一致：至少 10 位、含大写和小写字母、含数字 */
const RULES: { label: string; ok: (p: string) => boolean }[] = [
  { label: "至少 10 位", ok: (p) => p.length >= 10 },
  { label: "含大写和小写字母", ok: (p) => /[A-Z]/.test(p) && /[a-z]/.test(p) },
  { label: "含数字", ok: (p) => /\d/.test(p) },
];
export const passwordOk = (p: string, old?: string) => RULES.every((r) => r.ok(p)) && (!old || p !== old);

export function PasswordRules({ value, old }: { value: string; old?: string }) {
  const rules = [...RULES, ...(old ? [{ label: "与当前密码不同", ok: (p: string) => p.length > 0 && p !== old }] : [])];
  return (
    <div className={styles.rules}>
      {rules.map((r) => {
        const ok = r.ok(value);
        return (
          <span key={r.label} className={`${styles.rule} ${ok ? styles.ok : ""}`}>
            <span className={styles.dot}>{ok && <Icon name="check" size={10} />}</span>
            {r.label}
          </span>
        );
      })}
    </div>
  );
}

/**
 * 设置新密码（临时密码登录后必须先改）。
 *
 * oldPassword：刚在登录那一步输过的临时密码 —— 只在内存里带过来，不再让人重输；
 * 没有时（会话还在、被要求改密的情况）显示「当前密码」一栏。
 */
export function SetPasswordForm({ me, oldPassword, onDone, onBack }: { me: Me; oldPassword?: string; onDone: () => void; onBack?: () => void }) {
  const qc = useQueryClient();
  const [form] = Form.useForm<{ old_password?: string; new_password: string; again: string }>();
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const pw = Form.useWatch("new_password", form) ?? "";
  const old = Form.useWatch("old_password", form) ?? oldPassword;

  async function submit(v: { old_password?: string; new_password: string }) {
    setBusy(true);
    setError(null);
    try {
      await api.post("/auth/password", { old_password: oldPassword ?? v.old_password, new_password: v.new_password });
      await qc.invalidateQueries({ queryKey: ["me"] });
      onDone();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h1 className={styles.title}>设置新密码</h1>
      <p className={styles.sub}>这是临时密码，先换成你自己的密码再继续</p>
      <div className={styles.who}>
        <span className="ops-avatar" style={{ width: 30, height: 30, fontSize: 13, background: "#6366f1" }}>
          {me.name.slice(-1)}
        </span>
        <div className="ops-grow">
          <b>{me.name}</b>
          <div className="ops-sub">{me.account}</div>
        </div>
        {onBack && (
          <a onClick={onBack} style={{ fontSize: 13 }}>
            换个账号
          </a>
        )}
      </div>
      {!!error && <AuthError error={error} />}
      <Form form={form} layout="vertical" onFinish={submit} requiredMark={false}>
        {!oldPassword && (
          <Form.Item name="old_password" label="当前密码" rules={[{ required: true, message: "请输入当前密码" }]}>
            <Input.Password size="large" autoComplete="current-password" />
          </Form.Item>
        )}
        <Form.Item name="new_password" label="新密码" extra={<PasswordRules value={pw} old={old} />} rules={[{ required: true, message: "请输入新密码" }]}>
          <Input.Password size="large" autoComplete="new-password" autoFocus={!!oldPassword} />
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
          <Input.Password size="large" autoComplete="new-password" />
        </Form.Item>
        <Button type="primary" htmlType="submit" size="large" block loading={busy} disabled={!passwordOk(pw, old)}>
          保存并进入
        </Button>
      </Form>
    </>
  );
}
