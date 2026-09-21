"use client";

import { AntdRegistry } from "@ant-design/nextjs-registry";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App as AntdApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { useState, type ReactNode } from "react";

import { ApiError } from "@/lib/http";
import { atlasTheme } from "@/theme/antd";

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        // 4xx 是业务错误（not_found / conflict / 校验失败），重试没有意义，
        // 只会让用户多等三倍时间才看到同一条报错。
        retry: (failureCount, error) => {
          if (error instanceof ApiError && error.status < 500) return false;
          return failureCount < 2;
        },
        refetchOnWindowFocus: false,
      },
      mutations: { retry: false },
    },
  });
}

export function Providers({ children }: { children: ReactNode }) {
  // useState 而非模块级单例：模块级会在 SSR 时被多个请求共享，串数据。
  const [queryClient] = useState(makeQueryClient);

  return (
    <AntdRegistry>
      <ConfigProvider theme={atlasTheme} locale={zhCN} componentSize="middle">
        <AntdApp>
          <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
        </AntdApp>
      </ConfigProvider>
    </AntdRegistry>
  );
}
