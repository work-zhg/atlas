"use client";

/**
 * 接入的系统：部署配置里登记的系统（ATLAS_OPS_SYSTEMS）。运营中心用各自的运营密钥签名调它们的运营接口；
 * 能力决定这个系统出现在哪些页面。这里只看不改：改了要重新部署。
 */
import { Alert, Tag } from "antd";
import Link from "next/link";

import { ErrorBox, PageHead, SystemIcon } from "@/components/common";
import { Icon } from "@/components/Icon";
import { useSystems } from "@/lib/session";

/** 能力 → 名称、对应的页面（没有页面的只显示名称） */
const CAPS: Record<string, { label: string; href?: string }> = {
  user_center: { label: "租户与应用目录", href: "/apps" },
  lifecycle: { label: "租户生命周期" },
  capacity: { label: "份额与容量", href: "/capacity" },
  settings: { label: "系统参数", href: "/settings/params" },
  jobs: { label: "系统任务", href: "/settings/jobs" },
  runtime_env: { label: "沙箱模板", href: "/settings/templates" },
  skill_market: { label: "技能市场", href: "/settings/skills" },
  memory_scopes: { label: "记忆作用域", href: "/settings/scopes" },
};

export default function SystemsPage() {
  const q = useSystems();
  return (
    <>
      <PageHead title="接入的系统" sub="部署配置里登记的系统。运营中心用各自的运营密钥签名调它们的运营接口；能力决定这个系统出现在哪些页面" />
      <ErrorBox error={q.error} />
      <div className="ops-grid">
        {(q.data ?? []).map((s) => (
          <div key={s.code} className="ops-acard" style={{ cursor: "default" }}>
            <div className="ops-row" style={{ gap: 12 }}>
              <SystemIcon code={s.code} size={40} />
              <div className="ops-grow">
                <b>{s.name}</b>
                <div className="ops-sub ops-mono">{s.code}</div>
              </div>
              {s.key_configured ? (
                <Tag color="success" icon={<Icon name="check" size={12} style={{ marginRight: 4, verticalAlign: -2 }} />}>
                  密钥已配置
                </Tag>
              ) : (
                <Tag color="error" icon={<Icon name="alert" size={12} style={{ marginRight: 4, verticalAlign: -2 }} />}>
                  密钥没配
                </Tag>
              )}
            </div>
            <div className="ops-row" style={{ flexWrap: "wrap", gap: 6 }}>
              {s.capabilities.map((c) => {
                const cap = CAPS[c] ?? { label: c };
                return cap.href ? (
                  <Link key={c} href={cap.href} className="ops-chip">
                    {cap.label}
                  </Link>
                ) : (
                  <span key={c} className="ops-chip">
                    {cap.label}
                  </span>
                );
              })}
            </div>
            {!s.key_configured && (
              <div className="ops-sub" style={{ color: "var(--danger)" }}>
                运营中心调不了它：开通、停用、关闭租户到这一步会一直失败
              </div>
            )}
          </div>
        ))}
      </div>
      <Alert
        style={{ marginTop: 16 }}
        type="info"
        showIcon
        message="系统和密钥写在部署配置里（ATLAS_OPS_SYSTEMS、Secret atlas-ops-keys），这里只看不改：改了要重新部署"
      />
    </>
  );
}
