"use client";

import { Alert, Typography } from "antd";
import type { ReactNode } from "react";

import { ApiError } from "@/lib/api";

export function PageHead({ title, sub, extra }: { title: string; sub?: string; extra?: ReactNode }) {
  return (
    <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 20 }}>
      <div>
        <Typography.Title level={4} style={{ margin: 0 }}>
          {title}
        </Typography.Title>
        {sub && <Typography.Text type="secondary">{sub}</Typography.Text>}
      </div>
      {extra}
    </div>
  );
}

export function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message;
  return String(e);
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  return <Alert type="error" showIcon message={errorText(error)} style={{ marginBottom: 16 }} />;
}

/** 临时密码 / 新凭据只显示一次：醒目地给出来，提醒交出去。 */
export function OneTimeSecret({ label, value }: { label: string; value: string }) {
  return (
    <Alert
      type="warning"
      showIcon
      message={`${label}只显示这一次，关掉就再也看不到`}
      description={
        <Typography.Paragraph copyable={{ text: value }} style={{ margin: 0, fontFamily: "monospace", fontSize: 15 }}>
          {value}
        </Typography.Paragraph>
      }
    />
  );
}
