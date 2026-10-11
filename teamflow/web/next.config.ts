import path from "node:path";

import type { NextConfig } from "next";

// ★ 与后端同源：/api 经 Next 反向代理到 TeamFlow 后端（默认 :8040），会话 Cookie 与 CSRF 双提交都同源。
const API_ORIGIN = process.env.TF_API_ORIGIN ?? "http://127.0.0.1:8040";

// 部署在网关子路径下时（如 /uc、/teamflow）构建时设置 NEXT_PUBLIC_BASE_PATH；本地开发为空
const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

const config: NextConfig = {
  reactStrictMode: true,
  basePath: BASE_PATH || undefined,
  // 容器镜像只带 .next/standalone（自带最小 node_modules 与 server.js）
  output: "standalone",
  // 产物追踪根 = 前端 workspace 根（仓库根）：共享包 packages/chat-ui 与依赖都在 teamflow/web 之外
  outputFileTracingRoot: path.join(__dirname, "../.."),
  transpilePackages: ["@atlas/chat-ui", "antd", "@ant-design/icons", "rc-util", "rc-picker", "rc-tree", "rc-table"],
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_ORIGIN}/api/:path*` }];
  },
};

export default config;
