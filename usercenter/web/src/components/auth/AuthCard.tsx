"use client";

/**
 * 统一登录的外壳：品牌 + 居中卡片。登录页、设置新密码都用它。
 *
 * ★ 品牌写 Atlas，不写「用户中心」：员工是来登录 Atlas、TeamFlow 的，用户中心对他们是后台概念。
 */
import { useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Form, Input } from "antd";
import { useState, type ReactNode } from "react";

import { PersonAvatar } from "@/components/common";
import { Icon } from "@/components/Icon";
import { api, ApiError, errorText } from "@/lib/api";
import type { Me, PasswordRules } from "@/lib/types";

import styles from "./auth.module.css";

export function AuthCard({ children }: { children: ReactNode }) {
  return (
    <div className={styles.wrap}>
      <div className={styles.brand}>
        <span className={styles.logo}>A</span>Atlas
      </div>
      <div className={styles.card}>{children}</div>
      <div className={styles.foot}>统一身份认证 · 一个账号登录所有应用</div>
    </div>
  );
}

/** 后端错误码 → 提示的轻重：锁定、停用、到期这类「要找人处理」的用警告色 */
export function AuthError({ error }: { error: unknown }) {
  const code = error instanceof ApiError ? error.code : "";
  const warn = ["ACCOUNT_LOCKED", "TENANT_DISABLED", "TEMP_PASSWORD_EXPIRED"].includes(code);
  return <Alert type={warn ? "warning" : "error"} showIcon message={errorText(error)} style={{ marginBottom: 16 }} />;
}

function ruleList(r: PasswordRules, old: string | undefined): { label: string; ok: (p: string) => boolean }[] {
  return [
    { label: `至少 ${r.min_len} 位`, ok: (p) => p.length >= r.min_len },
    ...(r.need_upper ? [{ label: "含大写字母", ok: (p: string) => /[A-Z]/.test(p) }] : []),
    ...(r.need_digit ? [{ label: "含数字", ok: (p: string) => /\d/.test(p) }] : []),
    // 与后端一致：不是字母或数字的字符都算（后端用 str.isalnum）
    ...(r.need_symbol ? [{ label: "含特殊字符", ok: (p: string) => /[^\p{L}\p{N}]/u.test(p) }] : []),
    ...(old ? [{ label: "与当前密码不同", ok: (p: string) => p.length > 0 && p !== old }] : []),
  ];
}

/**
 * 设置新密码（首次登录 / 管理员重置后 / 策略要求）。
 *
 * oldPassword：刚在登录那一步输过的临时密码 —— 只在内存里带过来，不再让人重输；
 * 没有时（经网关 401 跳到 /change-password 的情况）显示「当前密码」一栏。
 */
export function SetPasswordForm({
  me,
  oldPassword,
  title,
  submitText,
  onDone,
  onSwitchAccount,
}: {
  me: Me;
  oldPassword?: string;
  /** 个人中心里主动改密时用「修改密码」；登录流程里缺省为「设置新密码」 */
  title?: string;
  submitText: string;
  onDone: () => void;
  /** 登录流程才有：顶部显示「是谁」并可以换账号 */
  onSwitchAccount?: () => void;
}) {
  const qc = useQueryClient();
  const [old, setOld] = useState(oldPassword ?? "");
  const [pw1, setPw1] = useState("");
  const [pw2, setPw2] = useState("");
  const [err, setErr] = useState<unknown>();
  const [loading, setLoading] = useState(false);
  const rules = ruleList(me.password_rules, old || undefined);
  const ready = !!old && rules.every((r) => r.ok(pw1)) && pw1 === pw2;

  async function submit() {
    setLoading(true);
    setErr(undefined);
    try {
      await api.post("/auth/password", { old_password: old, new_password: pw1 });
      await qc.invalidateQueries({ queryKey: ["me"] });
      onDone();
    } catch (e) {
      setErr(e);
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      {onSwitchAccount && (
        <div className={styles.who}>
          <PersonAvatar name={me.user.name} size={32} />
          <div className="uc-grow">
            <b>{me.user.name}</b>
            <div className="uc-sub">
              {me.user.account} · {me.tenant.name}
            </div>
          </div>
          <a onClick={onSwitchAccount}>不是你？</a>
        </div>
      )}
      <h1 className={styles.title}>{title ?? "设置新密码"}</h1>
      <p className={styles.sub}>
        {title ? `要求：${me.password_policy}` : me.first_login ? "首次登录，请把管理员给的临时密码换成你自己的密码" : "管理员要求你修改密码后再继续"}
      </p>
      {err !== undefined && <AuthError error={err} />}
      <Form layout="vertical" requiredMark={false} onFinish={() => ready && void submit()}>
        {!oldPassword && (
          <Form.Item label={title ? "当前密码" : "当前密码（临时密码）"}>
            <Input.Password size="large" autoComplete="current-password" value={old} onChange={(e) => setOld(e.target.value)} autoFocus />
          </Form.Item>
        )}
        <Form.Item label="新密码" style={{ marginBottom: 14 }}>
          <Input.Password size="large" autoComplete="new-password" value={pw1} onChange={(e) => setPw1(e.target.value)} autoFocus={!!oldPassword} />
          <div className={styles.rules} aria-live="polite">
            {rules.map((r) => (
              <span key={r.label} className={`${styles.rule} ${r.ok(pw1) ? styles.ok : ""}`}>
                <span className={styles.dot}>{r.ok(pw1) && <Icon name="check" size={10} />}</span>
                {r.label}
              </span>
            ))}
          </div>
        </Form.Item>
        <Form.Item label="确认新密码">
          <Input.Password size="large" autoComplete="new-password" value={pw2} onChange={(e) => setPw2(e.target.value)} />
          {pw2 && (
            <div className={`${styles.rule} ${pw1 === pw2 ? styles.ok : styles.bad}`} style={{ marginTop: 6 }}>
              {pw1 === pw2 ? (
                <>
                  <span className={styles.dot}>
                    <Icon name="check" size={10} />
                  </span>
                  两次输入一致
                </>
              ) : (
                <>
                  <Icon name="alert" size={14} />
                  两次输入不一致
                </>
              )}
            </div>
          )}
        </Form.Item>
        <Button type="primary" htmlType="submit" size="large" block loading={loading} disabled={!ready}>
          {submitText}
        </Button>
      </Form>
    </>
  );
}
