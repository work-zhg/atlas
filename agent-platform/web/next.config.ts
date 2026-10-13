import path from "node:path";

import type { NextConfig } from "next";

// 部署在网关子路径下时（如 /atlas）构建时设置 NEXT_PUBLIC_BASE_PATH；本地开发与现有部署为空
const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

const config: NextConfig = {
  reactStrictMode: true,
  basePath: BASE_PATH || undefined,
  // 容器镜像只带 .next/standalone（自带最小 node_modules 与 server.js）
  output: "standalone",
  // ★ 产物追踪根 = 前端 workspace 根（仓库根）：共享包 packages/chat-ui 与依赖都在 agent-platform/web/ 之外。
  //   不钉死的话 Next 会向上搜索 lockfile，可能一路找到 ~/package-lock.json。
  outputFileTracingRoot: path.join(__dirname, "../.."),
  // antd v5 的 ESM 产物需要 Next 转译，否则 App Router 下会报 ESM/CJS 混用
  transpilePackages: ["@atlas/chat-ui", "antd", "@ant-design/icons", "rc-util", "rc-picker", "rc-tree", "rc-table"],
};

export default config;
