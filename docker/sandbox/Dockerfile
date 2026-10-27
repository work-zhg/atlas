# 原生智能体的默认沙箱镜像（doc/sandbox-opensandbox-design.html §8）。
#
#   docker build -f docker/sandbox/Dockerfile -t atlas-sandbox:dev \
#     --build-arg HTTP_PROXY=http://host.docker.internal:58591 \
#     --build-arg HTTPS_PROXY=http://host.docker.internal:58591 docker/sandbox
#
# ★ 不含任何守护进程：OpenSandbox 的 execd 由 init 容器注入（任意 glibc 镜像都能当沙箱）。
#   这里只是「模型常用的工具都在」的一个合理默认。
# ★ 以 UID 1000 运行、根只读：可写区是 /workspace、/home/agent、/repos、/tmp（Pod 挂进来）。
FROM debian:trixie-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash ca-certificates curl wget git jq ripgrep less file unzip zip xz-utils procps \
        python3 python3-pip python3-venv \
        nodejs npm \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Debian 的 pip 默认拒绝装进系统环境（PEP 668）。根只读本来就装不进系统，
# 放开它是为了让 `pip install --user` 直接落到 HOME（/home/agent），而不是先报一屏错
ENV PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_USER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NPM_CONFIG_PREFIX=/home/agent/.npm-global \
    PATH=/home/agent/.local/bin:/home/agent/.npm-global/bin:$PATH \
    LANG=C.UTF-8

RUN useradd --uid 1000 --create-home --home-dir /home/agent --shell /bin/bash agent
USER 1000
WORKDIR /workspace
