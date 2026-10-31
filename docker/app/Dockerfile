# server、config、knowledge、memory、MCP 网关、用户中心与 AI TeamFlow 后端的运行镜像。
#
# ★ 一个镜像、多个入口：它们是同一个 workspace 里的包，依赖树高度重叠
#   （fastapi / uvicorn / pydantic），拆成多个镜像只是多构建几次。
#
# ★ 共享的只是**文件系统**，不是进程：各服务各自的 Deployment。
#   会话 Pod 由 OpenSandbox 创建（doc/sandbox-opensandbox-design.html），这些进程都不持有 K8s 权限。

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ★ 预置知识库用的 embedding 模型（知识库的 embedding_model 默认值）。
#
#   不预置的话，每个容器**首次**入库 / 检索时都要去 HuggingFace 下 90MB。
#   在受限网络下那是一次必然失败。
#
#   ★ 放在 fastembed 默认会找的 <tempdir>/fastembed_cache（Linux 容器里就是
#     /tmp/fastembed_cache）：知识库不设 ATLAS_KB_MODEL_CACHE_DIR 时就读这里。
#   ★ 与知识库的 embedding_model 必须一致，改一处就要改两处。
#   ★ 跨会话记忆不再用 embedding（改走记忆服务，doc/memory-design.html）；ARG 名沿用旧的，
#     构建脚本不用跟着改。
#
#   ★ 放在拷贝源码之前、且只装 fastembed：这一层不随代码变化而重建 —— 模型约 90MB，
#     从境外下载，每次都重下很慢。
ARG MEMORY_EMBED_MODEL=BAAI/bge-small-zh-v1.5
RUN --mount=type=cache,target=/root/.cache/pip pip install "fastembed>=0.8.0"
# ★ HF_HUB_DISABLE_XET：huggingface_hub 默认用 xet 协议传大文件，经代理时会卡在最后一个文件
#   不动（2026-10-11 实测）；关掉后走普通 HTTPS 下载。只作用于这一步，不进镜像环境。
RUN HF_HUB_DISABLE_XET=1 python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='${MEMORY_EMBED_MODEL}')" \
    && du -sh /tmp/fastembed_cache

WORKDIR /app
# ★ 第三方依赖单独一层，放在拷源码之前：只改代码时这一层命中缓存，只重装下面本仓的包。
#   原先源码在依赖之前拷入，改一行代码就要把全部依赖重装一遍（QEMU 交叉构建下每次 3–6 分钟）。
#   requirements.txt 改了（uv.lock 变了）这一层才重跑；pip 下载缓存放 BuildKit 缓存挂载，不进镜像层。
#
# ★ 版本按 uv.lock 固定（docker/app/requirements.sh 导出）。不固定的话 pip 装的是构建当天的最新版：
#   2026-10-21 查到服务器镜像有 74 个包与锁文件不同，跑的是一套测试从没覆盖过的组合 —— 其中
#   FastAPI 0.151 自带 OTel，把每 3 秒一次的 /readyz 全部送进了 Langfuse（占 trace 的 99% 以上）。
COPY docker/app/requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip pip install -r /tmp/requirements.txt

# ★ 源码按产品分目录（agent-platform / infra-platform / usercenter / teamflow），产品内再按
#   libs / services 分组；镜像内仍平铺在 /app 下（布局不变，K8s 清单里的工作目录 /app/server
#   不用跟着改）。所以 services/agent 拷进来仍叫 server/ —— 那是镜像布局，不是包名（atlas_agent）
COPY agent-platform/libs/acp/ acp/
COPY agent-platform/libs/jsonrpc/ jsonrpc/
COPY agent-platform/libs/host/ host/
COPY agent-platform/libs/engine/ engine/
COPY infra-platform/libs/redislock/ redislock/
COPY agent-platform/services/skill/ skill/
COPY agent-platform/services/knowledge/ knowledge/
COPY agent-platform/services/memory/ memory/
COPY agent-platform/services/mcpgateway/ mcpgateway/
COPY agent-platform/services/modelgateway/ modelgateway/
COPY agent-platform/services/agent/ server/
# 用户中心与 AI TeamFlow 后端：与 Atlas 共用依赖树（fastapi / sqlalchemy / httpx），同一个镜像、各自的 Deployment
COPY usercenter/services/usercenter/ usercenter/
COPY teamflow/services/teamflow/ teamflow/

# ★ 本仓的包一次装：它们彼此按名字依赖（server → engine/acp/redislock），
#   分开装的话 pip 会去 PyPI 找 atlas-engine，而那里没有这个包。
#
# ★ skill 要在 server 之前：server 依赖 atlas-skill（只用它的 schemas）。
#
# ★ --no-deps：第三方依赖上一层已经装好。pip check 兜底 —— pyproject 加了依赖却没重新导出
#   requirements.txt 时在这里构建失败，而不是到 Pod 里才 ImportError。
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps \
                ./jsonrpc ./host ./acp ./engine ./redislock ./skill ./knowledge ./memory ./mcpgateway \
                ./modelgateway \
                ./server \
                ./usercenter ./teamflow \
    && pip check


# alembic.ini 与 migrations/ 在包外（server/ 目录下），迁移要从这里跑。
WORKDIR /app/server

EXPOSE 8000 8020 8030 8040 8050 8060 8070 8090

# server 的入口。skill 由 Deployment 覆盖成 python -m atlas_skill serve（迁移：python -m atlas_skill migrate，
# 迁移脚本在包内，不依赖工作目录）。
# --timeout-graceful-shutdown：停机时等进行中响应的上限。会话 SSE 由交接逻辑主动关闭，
# 这里兜的是别的长请求（大文件下载）—— 不设的话 uvicorn 会无限等下去，拖到被 K8s 强杀
CMD ["uvicorn", "atlas_agent.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "70"]
