import path from "node:path";

import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // 不钉死的话 Next 会向上搜索 lockfile，一路找到 ~/package-lock.json 当工作区根，
  // 导致 build traces 收集范围错误。web/ 就是根。
  outputFileTracingRoot: path.join(__dirname),
  // antd v5 的 ESM 产物需要 Next 转译，否则 App Router 下会报 ESM/CJS 混用
  transpilePackages: ["antd", "@ant-design/icons", "rc-util", "rc-picker", "rc-tree", "rc-table"],
};

export default config;
