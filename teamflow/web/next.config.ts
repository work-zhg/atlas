import path from "node:path";

import type { NextConfig } from "next";

// ★ 与后端同源：/api 经 Next 反向代理到 TeamFlow 后端（默认 :8040），会话 Cookie 与 CSRF 双提交都同源。
const API_ORIGIN = process.env.TF_API_ORIGIN ?? "http://127.0.0.1:8040";

const config: NextConfig = {
  reactStrictMode: true,
  outputFileTracingRoot: path.join(__dirname),
  transpilePackages: ["antd", "@ant-design/icons", "rc-util", "rc-picker", "rc-tree", "rc-table"],
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_ORIGIN}/api/:path*` }];
  },
};

export default config;
