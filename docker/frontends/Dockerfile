# syntax=docker/dockerfile:1
# 四个前端（Atlas、用户中心、AI TeamFlow、运营中心）一个镜像：各自 Next standalone 产物放在 /app/<名>，
# Atlas、用户中心、TeamFlow 由同一个 Pod 里的三个容器分别启动（node /app/<名>/server.js，40-frontends.yaml）；
# 运营中心由自己的 Deployment 启动（49-opscenter-web.yaml）。
#
# 构建（仓库根目录）：
# ★ Atlas 与 TeamFlow 在前端 workspace（根 pnpm-workspace.yaml）里，共用 @atlas/chat-ui；用户中心独立。
#
#   docker build -f docker/frontends/Dockerfile -t atlas-frontends:dev .
#   （npm 源默认官方；需要换源时加 --build-arg NPM_REGISTRY=…）
#
# ★ 构建期的地址都是同源相对路径，镜像不绑定任何具体的后端地址。约定（c4-model/apigateway/code-routing.html）：
#   页面 /<名>-ui（basePath），接口 /<名>，都经 API 网关（infra-platform/gateway/routes.yaml）。

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

# ── 前端 workspace（Atlas + AI TeamFlow + 共享组件 @atlas/chat-ui、@atlas/appconfig-ui、@atlas/jobs-ui）：依赖只装一次
FROM base AS ws
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY agent-platform/web/package.json agent-platform/web/
COPY teamflow/web/package.json teamflow/web/
COPY opscenter/web/package.json opscenter/web/
COPY packages/chat-ui/package.json packages/chat-ui/
COPY packages/appconfig-ui/package.json packages/appconfig-ui/
COPY packages/jobs-ui/package.json packages/jobs-ui/
RUN --mount=type=cache,id=pnpm-store,target=/pnpm-store,sharing=locked \
    pnpm install --frozen-lockfile --store-dir /pnpm-store
COPY packages/ packages/
COPY agent-platform/web/ agent-platform/web/
COPY teamflow/web/ teamflow/web/
COPY opscenter/web/ opscenter/web/
ENV NEXT_TELEMETRY_DISABLED=1

# ── Atlas：页面 /atlas-ui；接口 /atlas、/skill、/knowledge、/mcp、/model
FROM ws AS atlas
ENV NEXT_PUBLIC_BASE_PATH=/atlas-ui \
    NEXT_PUBLIC_API_BASE=/atlas \
    NEXT_PUBLIC_SKILL_API_BASE=/skill \
    NEXT_PUBLIC_KNOWLEDGE_API_BASE=/knowledge \
    NEXT_PUBLIC_MCP_GATEWAY_API_BASE=/mcp \
    NEXT_PUBLIC_MODEL_GATEWAY_API_BASE=/model
RUN pnpm --filter atlas-web build

# ── 用户中心：页面 /uc-ui（统一登录页 /uc-ui/login）；接口 /uc
FROM base AS uc
COPY usercenter/web/package.json usercenter/web/pnpm-lock.yaml usercenter/web/pnpm-workspace.yaml ./
RUN --mount=type=cache,id=pnpm-store,target=/pnpm-store,sharing=locked \
    pnpm install --frozen-lockfile --store-dir /pnpm-store
COPY usercenter/web/ ./
ENV NEXT_PUBLIC_BASE_PATH=/uc-ui NEXT_PUBLIC_API_BASE=/uc NEXT_TELEMETRY_DISABLED=1
RUN pnpm build

# ── AI TeamFlow：页面 /teamflow-ui；接口 /teamflow。节点对话区是共享的 Atlas 对话组件，直连 Atlas 的接口
#    /atlas（开新一轮另经 TeamFlow）；「在 Agent 平台里编辑」链到 /atlas-ui
FROM ws AS tf
ENV NEXT_PUBLIC_BASE_PATH=/teamflow-ui \
    NEXT_PUBLIC_API_BASE=/teamflow \
    NEXT_PUBLIC_ATLAS_API_BASE=/atlas \
    NEXT_PUBLIC_ATLAS_UI_BASE=/atlas-ui
RUN pnpm --filter atlas-teamflow-web build

# ── 运营中心：页面 /opscenter-ui；接口 /opscenter（统一登录，网关验 JWT，c4-model/opscenter/design-opscenter.html）。
#   名字不能叫 ops：网关对含 /ops/ 段的路径一律 404
FROM ws AS ops
ENV NEXT_PUBLIC_BASE_PATH=/opscenter-ui \
    NEXT_PUBLIC_API_BASE=/opscenter
RUN pnpm --filter atlas-opscenter-web build

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
# 运营中心前端不在 atlas-frontends 的 Pod 里：它自己的 Deployment 用这个镜像起 /app/ops/opscenter/web/server.js
COPY --from=ops --chown=node:node /src/opscenter/web/.next/standalone ./ops
COPY --from=ops --chown=node:node /src/opscenter/web/.next/static ./ops/opscenter/web/.next/static
USER node
# 默认启动 Atlas；Pod 里每个容器用 command 指定自己的 server.js 与 PORT
CMD ["node", "/app/atlas/agent-platform/web/server.js"]
