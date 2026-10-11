"use client";

/**
 * 嵌入方用：给对话区套上 Atlas 的设计系统（深色 antd 主题 + 设计变量），
 * 宿主页面保持自己的主题不受影响。Atlas 前端全站已是这套主题，不需要它。
 */
import { ConfigProvider } from "antd";
import type { CSSProperties, ReactNode } from "react";

import { atlasTheme } from "../theme/antd";

export function AtlasChatTheme({ children, style }: { children: ReactNode; style?: CSSProperties }) {
  return (
    <ConfigProvider theme={atlasTheme}>
      <div
        className="atlas-chat-root"
        style={{
          background: "var(--bg)",
          color: "var(--fg)",
          borderRadius: 12,
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
          ...style,
        }}
      >
        {children}
      </div>
    </ConfigProvider>
  );
}
