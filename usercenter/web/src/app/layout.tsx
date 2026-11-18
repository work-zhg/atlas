import type { Metadata } from "next";

import "@ant-design/v5-patch-for-react-19";

import { Providers } from "@/lib/providers";
import "@/theme/tokens.css";
import "./globals.css";

export const metadata: Metadata = { title: "用户中心 · Atlas", description: "用户、组织与权限；各应用的统一登录" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
