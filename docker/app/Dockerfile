# server、cluster、config 与 knowledge 的运行镜像。
#
# ★ 一个镜像、两个入口：两者是同一个 workspace 里的包，依赖树高度重叠
#   （fastapi / uvicorn / pydantic），拆成两个镜像只是多构建一次。
#
# ★ 共享的只是**文件系统**，不是进程。cluster 是全仓唯一持有 K8s 写权限的
#   进程（cluster/api.py 的开头就是这句），把它与 server 合进一个进程等于
#   把那份 RBAC 一并发给 server。两个 Deployment、两个 ServiceAccount。

FROM python:3.13-slim

# ★ MEM0_TELEMETRY=False：mem0 默认往 PostHog（us.i.posthog.com）上报使用
#   数据。自托管平台不该有这种出站，在受限网络里它还是个无谓的超时源。
#   代码里也设了一道（memory/client.py，那个开关是 import 时读的）。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MEM0_TELEMETRY=False

# ★ 预置记忆用的 embedding 模型（记忆设计 §12 第 3 项）。
#
#   不预置的话，每个容器**首次**用到记忆时都要去 HuggingFace 下 90MB。
#   在受限网络下那是一次必然失败，而表现是「记忆悄悄不工作」——
#   会话照常、日志里一条告警，没人会注意到。
#
#   ★ 路径不能自己挑：mem0 的 fastembed embedder 不转发 cache_dir，
#     所以只能放到 fastembed 默认会找的 <tempdir>/fastembed_cache
#     （Linux 容器里就是 /tmp/fastembed_cache）。
#
#   ★ 与 Settings.memory_embed_model 必须一致，改一处就要改两处。
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
COPY acp/ acp/
COPY jsonrpc/ jsonrpc/
COPY host/ host/
COPY engine/ engine/
COPY cluster/ cluster/
COPY config/ config/
COPY knowledge/ knowledge/
COPY server/ server/

# ★ 四个包一次装：它们彼此按名字依赖（server → engine/acp/cluster），
#   分开装的话 pip 会去 PyPI 找 atlas-engine，而那里没有这个包。
#
# ★ cluster[k8s]：KubernetesBackend 的客户端是可选依赖，不装就只剩内存
#   后端 —— 而内存后端「起来了但什么都没真的创建」，acp 的每一轮都会超时。
#
# ★ config 要在 server 之前：server 依赖 atlas-config（只用它的 schemas）。
#
# ★ pip 下载缓存放 BuildKit 缓存挂载（不进镜像层）：源码在依赖之前拷入，任何代码改动都会让这一层
#   重跑，有了缓存就不必每次重新下载全部依赖。
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install ./jsonrpc ./host ./acp ./engine "./cluster[k8s]" ./config ./knowledge ./server


# alembic.ini 与 migrations/ 在包外（server/ 目录下），迁移要从这里跑。
WORKDIR /app/server

EXPOSE 8000 8010 8020

# server 的入口。cluster 由 Deployment 覆盖成 python -m atlas_cluster；
# config 覆盖成 python -m atlas_config serve（迁移：python -m atlas_config migrate，
# 迁移脚本在包内，不依赖工作目录）。
CMD ["uvicorn", "atlas_server.main:app", "--host", "0.0.0.0", "--port", "8000"]
