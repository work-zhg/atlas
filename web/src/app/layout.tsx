import type { Metadata, Viewport } from "next";

// antd v5 在 React 19 下需要这个补丁，否则 message/notification/Modal 的
// 静态方法（App.useApp 之外的调用路径）会报 unmountComponentAtNode 不存在。
import "@ant-design/v5-patch-for-react-19";

import { Rail } from "@/components/Rail";
import { Providers } from "@/lib/providers";
import "./globals.css";

export const metadata: Metadata = {
  title: "Atlas",
  description: "AI agent 平台 —— 对话与智能体管理",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // 不设 maximum-scale / user-scalable=no —— 禁用缩放违反 a11y
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <Providers>
          {/* Console Shell 三段式的最外层：Rail + 内容区（MASTER.md §1） */}
          <div style={{ display: "flex", height: "100vh", overflow: "hidden" }}>
            <Rail />
            <div style={{ flex: 1, minWidth: 0, display: "flex" }}>{children}</div>
          </div>
        </Providers>
      </body>
    </html>
  );
}
