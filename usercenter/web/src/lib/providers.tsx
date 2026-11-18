"use client";

import { AntdRegistry } from "@ant-design/nextjs-registry";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App as AntdApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { useState, type ReactNode } from "react";

import { ucTheme } from "@/theme/antd";

import { ApiError } from "./api";

export function Providers({ children }: { children: ReactNode }) {
  // useState 而非模块级单例：模块级会在 SSR 时被多个请求共享
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 10_000,
            retry: (n, e) => !(e instanceof ApiError && e.status < 500) && n < 2,
            refetchOnWindowFocus: false,
          },
          mutations: { retry: false },
        },
      }),
  );
  return (
    <AntdRegistry>
      <ConfigProvider locale={zhCN} theme={ucTheme}>
        <AntdApp>
          <QueryClientProvider client={client}>{children}</QueryClientProvider>
        </AntdApp>
      </ConfigProvider>
    </AntdRegistry>
  );
}
