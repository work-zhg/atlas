import path from "node:path";

import type { NextConfig } from "next";

// ★ 管理台与后端同源：/api 经 Next 反向代理到用户中心后端（默认 :8030）。
//   这样会话 Cookie 与 CSRF 双提交都是同源的，不必处理跨域 Cookie。
const API_ORIGIN = process.env.UC_API_ORIGIN ?? "http://127.0.0.1:8030";

const config: NextConfig = {
  reactStrictMode: true,
  outputFileTracingRoot: path.join(__dirname),
  transpilePackages: ["antd", "@ant-design/icons", "rc-util", "rc-picker", "rc-tree", "rc-table"],
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_ORIGIN}/api/:path*` }];
  },
};

export default config;
