"use client";

/**
 * 注入点：对话组件向哪个 Atlas 端点发请求、带什么头。
 *
 * ★ 组件与 hooks 不读任何环境变量 —— 同一套代码，Atlas 前端、嵌入方（AI TeamFlow）都注入经网关的
 *   Atlas 接口；嵌入方要自己开新一轮时另给 ThreadConversation 的 submitRun。
 */
import { createContext, useContext, useMemo, type ReactNode } from "react";

import { makeHttp, type AtlasClientConfig, type Http } from "./lib/http";

const ClientContext = createContext<AtlasClientConfig | null>(null);

export function AtlasClientProvider({ config, children }: { config: AtlasClientConfig; children: ReactNode }) {
  return <ClientContext.Provider value={config}>{children}</ClientContext.Provider>;
}

export function useAtlasClient(): AtlasClientConfig {
  const c = useContext(ClientContext);
  if (!c) throw new Error("缺少 <AtlasClientProvider>：对话组件需要知道 Atlas 接口地址");
  return c;
}

export function useAtlasHttp(): Http {
  const c = useAtlasClient();
  return useMemo(() => makeHttp(c), [c]);
}
