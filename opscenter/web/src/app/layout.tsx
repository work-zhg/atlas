import type { Metadata } from "next";

import "@ant-design/v5-patch-for-react-19";

import { Providers } from "@/lib/providers";
import "@/theme/tokens.css";
import "./globals.css";

export const metadata: Metadata = { title: "运营中心", description: "管平台本身：租户、应用目录、容量、运行环境" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
