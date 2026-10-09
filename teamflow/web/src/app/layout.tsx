import type { Metadata } from "next";

import "@ant-design/v5-patch-for-react-19";

import { Providers } from "@/lib/providers";
import "./globals.css";

export const metadata: Metadata = { title: "AI TeamFlow", description: "让 AI Agent 作为团队成员参与研发流程" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
