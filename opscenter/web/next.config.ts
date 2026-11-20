import path from "node:path";

import type { NextConfig } from "next";

// ★ 与后端同源：/api 经 Next 反向代理到运营服务（默认 :8100），会话 Cookie 与 CSRF 双提交都同源。
//   运营中心有自己的入口，租户的网关不转发它（c4-model/opscenter/container.html）。
const API_ORIGIN = process.env.OPS_API_ORIGIN ?? "http://127.0.0.1:8100";

// 部署在子路径下时构建时设置 NEXT_PUBLIC_BASE_PATH；本地开发为空
const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

const config: NextConfig = {
  reactStrictMode: true,
  basePath: BASE_PATH || undefined,
  output: "standalone",
  // 产物追踪根 = 前端 workspace 根（仓库根）：共享包 packages/* 与依赖都在 opscenter/web 之外
  outputFileTracingRoot: path.join(__dirname, "../.."),
  transpilePackages: ["@atlas/appconfig-ui", "@atlas/jobs-ui", "antd", "@ant-design/icons", "rc-util", "rc-picker", "rc-tree", "rc-table"],
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_ORIGIN}/api/:path*` }];
  },
};

export default config;
