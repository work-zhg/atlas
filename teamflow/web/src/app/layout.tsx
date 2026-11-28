import type { Metadata } from "next";

import "@ant-design/v5-patch-for-react-19";

import { Providers } from "@/lib/providers";
// Atlas 的设计变量（和 Atlas 同一套浅色）：页面和对话组件共用
import "@atlas/chat-ui/src/theme/tokens.css";
import "./globals.css";

export const metadata: Metadata = { title: "AI TeamFlow", description: "面向团队协作的 AI 工作流平台" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
