"use client";

/**
 * 统一登录：Atlas、TeamFlow 未登录都跳到 /uc/login?next=<原页面>，登录后回去。
 *
 * - 企业：单租户部署不用填企业代码，只展示企业名；多租户记住上次的企业（显示企业名，可「更换」），只有第一次要输入。
 * - 须改密（首次登录、管理员重置后）：同一张卡片进入第二步设置新密码；刚输过的临时密码在内存里带过去。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Form, Input, Popover } from "antd";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";

import { AuthCard, AuthError, SetPasswordForm } from "@/components/auth/AuthCard";
import styles from "@/components/auth/auth.module.css";
import { SquareIcon } from "@/components/common";
import { Icon } from "@/components/Icon";
import { api } from "@/lib/api";
import { goNext, nextTarget, useMe, withNext } from "@/lib/session";
import type { Me } from "@/lib/types";

const TENANT_KEY = "uc_tenant";
const TENANT_NAME_KEY = "uc_tenant_name";

interface Remembered {
  code: string;
  name: string | null;
}

function remembered(): Remembered | null {
  if (typeof window === "undefined") return null;
  const code = localStorage.getItem(TENANT_KEY);
  return code ? { code, name: localStorage.getItem(TENANT_NAME_KEY) } : null;
}

export default function LoginPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const [err, setErr] = useState<unknown>();
  const [loading, setLoading] = useState(false);
  const [caps, setCaps] = useState(false);
  const [tenant, setTenant] = useState<Remembered | null>(null);
  const [step, setStep] = useState<{ me: Me; old: string } | null>(null);
  const [hasNext, setHasNext] = useState(false);
  // 提交过程中自己处理须改密（进第二步），不要被下面的跳转抢先
  const submitting = useRef(false);
  useEffect(() => {
    setTenant(remembered());
    setHasNext(!!nextTarget());
  }, []);
  const needTenant = useQuery({
    queryKey: ["tenant-required"],
    // tenant：单租户时是那家企业（只给名称），多租户时为空
    queryFn: () => api.get<{ required: boolean; tenant: { name: string } | null }>("/auth/tenant-required"),
  });
  // 已登录但须先改密（各应用经网关拿到 401 跳来的）：直接去改密，改完回原页面
  const me = useMe();
  useEffect(() => {
    if (me.data?.must_change_password && !step && !submitting.current) router.replace(withNext("/change-password"));
  }, [me.data, step, router]);

  function done() {
    if (!goNext()) router.replace("/");
  }

  async function submit(values: { tenant?: string; account: string; password: string }) {
    submitting.current = true;
    setLoading(true);
    setErr(undefined);
    const code = needTenant.data?.required ? (tenant?.code ?? values.tenant?.trim().toLowerCase()) : undefined;
    try {
      const r = await api.post<{ must_change_password: boolean }>("/auth/login", { account: values.account, password: values.password, tenant: code });
      qc.removeQueries({ queryKey: ["me"] });
      const who = await qc.fetchQuery({ queryKey: ["me"], queryFn: () => api.get<Me>("/auth/me") });
      if (code) {
        localStorage.setItem(TENANT_KEY, code);
        localStorage.setItem(TENANT_NAME_KEY, who.tenant.name);
      }
      if (r.must_change_password) setStep({ me: who, old: values.password });
      else done();
    } catch (e) {
      setErr(e);
    } finally {
      submitting.current = false;
      setLoading(false);
    }
  }

  async function switchAccount() {
    await api.post("/auth/logout").catch(() => undefined);
    qc.clear();
    setStep(null);
  }

  const onKey = (e: KeyboardEvent) => setCaps(e.getModifierState("CapsLock"));

  if (step)
    return (
      <AuthCard>
        <SetPasswordForm me={step.me} oldPassword={step.old} submitText="设置并进入" onDone={done} onSwitchAccount={() => void switchAccount()} />
      </AuthCard>
    );

  const showTenantInput = needTenant.data?.required && !tenant;
  // 单租户：只展示企业名，不能更换
  const soleTenant = needTenant.data?.required ? null : needTenant.data?.tenant;
  return (
    <AuthCard>
      {hasNext && (
        <div className={styles.from}>
          <Icon name="arrowRight" size={14} />
          登录后回到刚才的页面
        </div>
      )}
      <h1 className={styles.title}>登录</h1>
      <p className={styles.sub}>使用公司账号登录，一个账号通用所有应用</p>
      {err !== undefined && <AuthError error={err} />}
      <Form layout="vertical" onFinish={submit} requiredMark={false}>
        {needTenant.data?.required && tenant && (
          <Form.Item label="企业">
            <div className={styles.tenant}>
              <SquareIcon icon="building" size={32} />
              <div className="uc-grow">
                <b>{tenant.name ?? tenant.code}</b>
                <div className="uc-sub uc-mono">{tenant.code}</div>
              </div>
              <a onClick={() => setTenant(null)}>更换</a>
            </div>
          </Form.Item>
        )}
        {soleTenant && (
          <Form.Item label="企业">
            <div className={styles.tenant}>
              <SquareIcon icon="building" size={32} />
              <b className="uc-grow">{soleTenant.name}</b>
            </div>
          </Form.Item>
        )}
        {showTenantInput && (
          <Form.Item name="tenant" label="企业代码" rules={[{ required: true, message: "请输入企业代码" }]}>
            <Input size="large" autoComplete="organization" autoFocus placeholder="由管理员提供" />
          </Form.Item>
        )}
        <Form.Item name="account" label="账号" rules={[{ required: true, message: "请输入账号" }]}>
          <Input size="large" autoComplete="username" autoFocus={!showTenantInput} onKeyUp={onKey} />
        </Form.Item>
        <Form.Item
          name="password"
          label="密码"
          rules={[{ required: true, message: "请输入密码" }]}
          extra={caps ? <span className={styles.caps}>大写锁定已打开</span> : undefined}
        >
          <Input.Password size="large" autoComplete="current-password" onKeyUp={onKey} onKeyDown={onKey} />
        </Form.Item>
        <Button type="primary" htmlType="submit" size="large" block loading={loading}>
          登录
        </Button>
      </Form>
      <div className={styles.help}>
        <Popover
          trigger="click"
          content={<div style={{ maxWidth: 260 }}>密码忘了请联系所在企业的管理员重置。重置后你会拿到一个临时密码，登录时会让你改成自己的密码。</div>}
        >
          <a>忘记密码？</a>
        </Popover>
        {showTenantInput && <span className="uc-muted">不知道企业代码？问管理员</span>}
      </div>
    </AuthCard>
  );
}
