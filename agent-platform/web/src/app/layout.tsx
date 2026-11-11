import type { Metadata, Viewport } from "next";

// antd v5 在 React 19 下需要这个补丁，否则 message/notification/Modal 的
// 静态方法（App.useApp 之外的调用路径）会报 unmountComponentAtNode 不存在。
import "@ant-design/v5-patch-for-react-19";

import { AuthGate } from "@/components/AuthGate";
import { AppShell } from "@/components/shell/AppShell";
import { Providers } from "@/lib/providers";
import "@atlas/chat-ui/src/theme/tokens.css";
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
          {/* 登录守卫在最外层：没登录不渲染导航与页面（权限设计 §13） */}
          <AuthGate>
            {/* 顶部一级菜单（平台）+ 左侧二级菜单 + 内容区 */}
            <AppShell>{children}</AppShell>
          </AuthGate>
        </Providers>
      </body>
    </html>
  );
}
