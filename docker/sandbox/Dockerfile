# 原生智能体的默认沙箱镜像（doc/sandbox-opensandbox-design.html §8）。
#
#   docker build -f docker/sandbox/Dockerfile -t atlas-sandbox:dev \
#     --build-arg HTTP_PROXY=http://host.docker.internal:58591 \
#     --build-arg HTTPS_PROXY=http://host.docker.internal:58591 docker/sandbox
#
# ★ 不含任何守护进程：OpenSandbox 的 execd 由 init 容器注入（任意 glibc 镜像都能当沙箱）。
#   这里只是「模型常用的工具都在」的一个合理默认。
# ★ 以 UID 1000 运行、根可写、免密 sudo（开放档，sbx-strict.yaml）：要装系统包就 sudo apt-get。
#   搞坏了重建沙箱就恢复；/home/agent、/repos、/tmp 是 Pod 挂进来的 emptyDir，/data 是这个人的存储。
FROM debian:trixie-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash ca-certificates curl wget git jq ripgrep less file unzip zip xz-utils procps sudo \
        python3 python3-pip python3-venv \
        nodejs npm \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Debian 的 pip 默认拒绝装进系统环境（PEP 668）。放开它是为了让 `pip install`
# 直接落到 HOME（/home/agent），而不是先报一屏错
ENV PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_USER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NPM_CONFIG_PREFIX=/home/agent/.npm-global \
    PATH=/home/agent/.local/bin:/home/agent/.npm-global/bin:$PATH \
    LANG=C.UTF-8

RUN useradd --uid 1000 --create-home --home-dir /home/agent --shell /bin/bash agent \
    && echo 'agent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agent \
    && chmod 0440 /etc/sudoers.d/agent
USER 1000
WORKDIR /workspace
