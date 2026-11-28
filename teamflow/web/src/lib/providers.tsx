"use client";

import { AntdRegistry } from "@ant-design/nextjs-registry";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App as AntdApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { useState, type ReactNode } from "react";

import { AtlasClientProvider, type AtlasClientConfig } from "@atlas/chat-ui";
import { atlasTheme } from "@atlas/chat-ui/src/theme/antd";

import { ATLAS_API_BASE, refreshSession, writeHeaders } from "./api";

/**
 * 节点对话组件直连 Agent 平台（经网关 /atlas）：读对话、事件流、回答、接管、停止都不经 TeamFlow。
 * 开新一轮另经 TeamFlow（ThreadConversation 的 submitRun）。
 */
const atlasClient: AtlasClientConfig = { apiBase: ATLAS_API_BASE, headers: writeHeaders, refresh: refreshSession };

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        // 4xx 是业务错误（看不到、冲突、校验失败），重试只会让人多等；TeamFlow 和 Agent 平台的错误都带 status
        retry: (n, e) => !((e as { status?: number }).status! < 500) && n < 2,
        refetchOnWindowFocus: false,
      },
      mutations: { retry: false },
    },
  });
}

export function Providers({ children }: { children: ReactNode }) {
  // useState 而非模块级单例：模块级会在 SSR 时被多个请求共享
  const [client] = useState(makeQueryClient);
  return (
    <AntdRegistry>
      <ConfigProvider theme={atlasTheme} locale={zhCN} button={{ autoInsertSpace: false }}>
        <AntdApp>
          <QueryClientProvider client={client}>
            <AtlasClientProvider config={atlasClient}>{children}</AtlasClientProvider>
          </QueryClientProvider>
        </AntdApp>
      </ConfigProvider>
    </AntdRegistry>
  );
}
