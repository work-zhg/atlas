"use client";

import { Button, Result } from "antd";

import { ApiError } from "@/lib/api";

/** 会触发跳转的认证类错误（交给调用方跳登录 / 改密），其余错误要显示出来，而不是一直转圈。 */
export function isAuthRedirect(e: unknown): boolean {
  return e instanceof ApiError && (e.status === 401 || e.code === "PASSWORD_CHANGE_REQUIRED");
}

/** 后端不可用（502 / 503 / 网络错误等）：说明原因并可重试。 */
export function BackendDown({ error, onRetry, retrying }: { error: unknown; onRetry: () => void; retrying?: boolean }) {
  const status = error instanceof ApiError ? error.status : 0;
  const gateway = status === 502 || status === 503 || status === 504;
  return (
    <Result
      status="warning"
      style={{ marginTop: 80 }}
      title={gateway || !status ? "服务暂时不可用" : "加载失败"}
      subTitle={
        gateway || !status
          ? "后端服务没有响应（可能未启动或正在重启），请稍后重试；持续出现请联系管理员。"
          : error instanceof Error
            ? error.message
            : String(error)
      }
      extra={
        <Button type="primary" loading={retrying} onClick={onRetry}>
          重试
        </Button>
      }
    />
  );
}
