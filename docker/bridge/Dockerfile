# 会话 Pod 的镜像：bridge（Python）+ ACP adapter（Node CLI）。
#
# ★ 一个镜像里两套运行时，是因为 acp 详设 §04 的那条分工：bridge 是 Pod 的
#   1 号进程、说 WS 上的线协议；adapter 是它拉起的子进程、说 stdio 上的
#   JSON-RPC。两者必须同生共死，所以打在一起而不是拆两个容器 —— 拆开的话
#   "adapter 崩了"要靠跨容器信号去发现，而那条路没法保证时序。
#
# ★ 镜像即版本组合。cli_type / adapter / image 三个字段一起进 agent 版本快照
#   且不可改（domain/spec.py::CliSpec）—— 换 CLI 版本就是换镜像 tag，
#   而不是原地升级：原地改等于篡改存量会话的历史行为。

# ── Node 运行时 ──────────────────────────────────────────────────────
# ★ 必须与最终镜像同一个 Debian 版本（trixie）。跨版本拷 node 会缺
#   libatomic 之类的动态库 —— 而它的表现是容器起来之后 adapter 才
#   "error while loading shared libraries"，离根因很远。手工拼 node 进
#   python 镜像时踩过一次。
FROM node:24-trixie-slim AS node

# ── adapter：把 npm 包装到一个独立前缀 ────────────────────────────────
# ★ 0.3.0 起换成 @agentclientprotocol/claude-agent-acp（原 @zed-industries/claude-code-acp
#   已停更于 0.16.2）。新包才有 auto 权限模式（doc/acp-permission-mode-design.html）。
#
# ★ 两个入口：
#   /opt/acp-cli/bin/claude-acp         稳定入口。新建的 agent 用它，以后换 adapter 不用改 spec
#   旧包的 dist/index.js                 软链接到新包。存量 agent 的 cli.adapter 写死了这个路径，
#                                       没有它们会全部起不来（Node 按真实路径解析 import，软链接可用）
FROM node AS adapter
ARG ACP_ADAPTER_VERSION=0.85.1
RUN npm install --global --prefix /opt/acp-cli \
        "@agentclientprotocol/claude-agent-acp@${ACP_ADAPTER_VERSION}" \
    && npm cache clean --force \
    && NEW=/opt/acp-cli/lib/node_modules/@agentclientprotocol/claude-agent-acp/dist/index.js \
    && ln -s "$NEW" /opt/acp-cli/bin/claude-acp \
    && mkdir -p /opt/acp-cli/lib/node_modules/@zed-industries/claude-code-acp/dist \
    && ln -s "$NEW" /opt/acp-cli/lib/node_modules/@zed-industries/claude-code-acp/dist/index.js

# ── bridge 的 Python 依赖 ────────────────────────────────────────────
FROM python:3.14-slim AS wheels
WORKDIR /build
COPY acp/ acp/
COPY bridge/ bridge/
# bridge 依赖的另外两个包：JSON-RPC 基础与上游协议模型（代码设计 §1）。
COPY jsonrpc/ jsonrpc/
COPY host/ host/
# ★ --no-cache-dir：这一层只为产出 wheel，缓存进不了最终镜像也没用，
#   留着只是让中间层更大。
RUN pip install --no-cache-dir --upgrade pip build \
    && python -m build --wheel --outdir /wheels jsonrpc \
    && python -m build --wheel --outdir /wheels host \
    && python -m build --wheel --outdir /wheels acp \
    && python -m build --wheel --outdir /wheels bridge

# ── 最终镜像 ────────────────────────────────────────────────────────
FROM python:3.14-slim

LABEL org.opencontainers.image.title="atlas-acp-bridge" \
      org.opencontainers.image.description="Atlas 会话 Pod：bridge + Claude Code ACP adapter"

# Node 运行时。只拷 /usr/local 下的那份 —— npm/npx 一并带上，
# adapter 的 shebang 要用到。
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

COPY --from=adapter /opt/acp-cli /opt/acp-cli

COPY --from=wheels /wheels /tmp/wheels
RUN pip install --no-cache-dir /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels

# ★ 必须是**数字** UID。只写 runAsNonRoot、镜像里没有数字 USER 的话，
#   kubelet 在启动前判定不了镜像是不是 root，一律拒绝启动
#   （CreateContainerConfigError）。模板那边也显式给了 runAsUser，
#   两边说同一件事。
#
# ★ HOME 指向 /state：根文件系统是只读的（模板里 readOnlyRootFilesystem），
#   而 CLI 要往 HOME 写会话记录。/state 由挂载器挂到对象存储上 ——
#   resume 跨 Pod 重建就靠它。
RUN useradd --uid 1000 --create-home --home-dir /home/agent agent \
    && mkdir -p /workspace /skills /state \
    && chown agent:agent /workspace /skills /state
USER 1000:1000
ENV HOME=/state \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace

# agent（ACP 适配器）的默认启动命令。模板用 ATLAS_BRIDGE_AGENT_CMD 覆盖它 ——
# 放在这里是为了让镜像自己能说清"我带的是哪个 adapter"。
ENV ATLAS_BRIDGE_AGENT_CMD="node /opt/acp-cli/bin/claude-acp"

EXPOSE 8900
# ★ 用 exec 形式：shell 形式会让 PID 1 变成 sh，SIGTERM 到不了 bridge，
#   它就没法在收到信号时关闭连接、终止 agent 的进程组。
CMD ["python", "-m", "atlas_bridge"]
