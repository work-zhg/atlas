import path from "node:path";

import type { NextConfig } from "next";

// ★ 管理台与后端同源：/api 经 Next 反向代理到用户中心后端（默认 :8030）。
//   这样会话 Cookie 与 CSRF 双提交都是同源的，不必处理跨域 Cookie。
const API_ORIGIN = process.env.UC_API_ORIGIN ?? "http://127.0.0.1:8030";

// 部署在网关子路径下时（如 /uc、/teamflow）构建时设置 NEXT_PUBLIC_BASE_PATH；本地开发为空
const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

const config: NextConfig = {
  reactStrictMode: true,
  basePath: BASE_PATH || undefined,
  // 容器镜像只带 .next/standalone（自带最小 node_modules 与 server.js）
  output: "standalone",
  outputFileTracingRoot: path.join(__dirname),
  transpilePackages: ["antd", "@ant-design/icons", "rc-util", "rc-picker", "rc-tree", "rc-table"],
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_ORIGIN}/api/:path*` }];
  },
};

export default config;
