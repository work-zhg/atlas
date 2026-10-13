# syntax=docker/dockerfile:1
# 三个前端（Atlas、用户中心、AI TeamFlow）一个镜像：各自 Next standalone 产物放在 /app/<名>，
# 由同一个 Pod 里的三个容器分别启动（node /app/<名>/server.js），前面是 nginx 容器按路径路由。
#
# 构建（仓库根目录）：
# ★ Atlas 与 TeamFlow 在前端 workspace（根 pnpm-workspace.yaml）里，共用 @atlas/chat-ui；用户中心独立。
#
#   docker build -f docker/frontends/Dockerfile -t atlas-frontends:dev .
#   （npm 源默认官方；需要换源时加 --build-arg NPM_REGISTRY=…）
#
# ★ 构建期的地址都是同源相对路径（由 nginx 转发），镜像不绑定任何具体的后端地址。

FROM node:22-alpine AS base
# npm 源（构建期）。lockfile 只记 integrity 不记源地址，换源不影响可复现性。
ARG NPM_REGISTRY=https://registry.npmjs.org
# ★ npm 认 npm_config_*，pnpm 11 只认 pnpm_config_*：两套都设
ENV npm_config_registry=$NPM_REGISTRY \
    pnpm_config_registry=$NPM_REGISTRY \
    pnpm_config_fetch_timeout=300000 \
    pnpm_config_fetch_retries=5
# ★ 三个应用共用一份 pnpm 下载缓存（BuildKit cache mount，不进镜像层）：
#   Docker Desktop 的容器网络经其内置代理，下载大包很慢；只有第一次构建需要下载。
RUN npm install -g pnpm@11.7.0
WORKDIR /src

# ── 前端 workspace（Atlas + AI TeamFlow + 共享对话组件 @atlas/chat-ui）：依赖只装一次
FROM base AS ws
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY agent-platform/web/package.json agent-platform/web/
COPY teamflow/web/package.json teamflow/web/
COPY packages/chat-ui/package.json packages/chat-ui/
RUN --mount=type=cache,id=pnpm-store,target=/pnpm-store,sharing=locked \
    pnpm install --frozen-lockfile --store-dir /pnpm-store
COPY packages/ packages/
COPY agent-platform/web/ agent-platform/web/
COPY teamflow/web/ teamflow/web/
ENV NEXT_TELEMETRY_DISABLED=1

# ── Atlas：/atlas；接口经 nginx 的 /atlas-api、/atlas-config-api 同源访问
FROM ws AS atlas
ENV NEXT_PUBLIC_BASE_PATH=/atlas \
    NEXT_PUBLIC_API_BASE=/atlas-api \
    NEXT_PUBLIC_CONFIG_API_BASE=/atlas-config-api \
    NEXT_PUBLIC_KNOWLEDGE_API_BASE=/atlas-knowledge-api
RUN pnpm --filter atlas-web build

# ── 用户中心：/uc
FROM base AS uc
COPY usercenter/web/package.json usercenter/web/pnpm-lock.yaml usercenter/web/pnpm-workspace.yaml ./
RUN --mount=type=cache,id=pnpm-store,target=/pnpm-store,sharing=locked \
    pnpm install --frozen-lockfile --store-dir /pnpm-store
COPY usercenter/web/ ./
ENV NEXT_PUBLIC_BASE_PATH=/uc NEXT_TELEMETRY_DISABLED=1
RUN pnpm build

# ── AI TeamFlow：/teamflow（人机协同对话区即共享的 Atlas 对话组件）
FROM ws AS tf
ENV NEXT_PUBLIC_BASE_PATH=/teamflow
RUN pnpm --filter atlas-teamflow-web build

# ── 运行镜像：只带 standalone 产物与静态资源（没有 pnpm、源码、构建缓存）
FROM node:22-alpine
ENV NODE_ENV=production HOSTNAME=0.0.0.0 NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
# workspace 下 standalone 按仓库相对路径展开：入口在 atlas/agent-platform/web/server.js、tf/teamflow/web/server.js
COPY --from=atlas --chown=node:node /src/agent-platform/web/.next/standalone ./atlas
COPY --from=atlas --chown=node:node /src/agent-platform/web/.next/static ./atlas/agent-platform/web/.next/static
COPY --from=uc --chown=node:node /src/.next/standalone ./uc
COPY --from=uc --chown=node:node /src/.next/static ./uc/.next/static
COPY --from=tf --chown=node:node /src/teamflow/web/.next/standalone ./tf
COPY --from=tf --chown=node:node /src/teamflow/web/.next/static ./tf/teamflow/web/.next/static
USER node
# 默认启动 Atlas；Pod 里每个容器用 command 指定自己的 server.js 与 PORT
CMD ["node", "/app/atlas/agent-platform/web/server.js"]
