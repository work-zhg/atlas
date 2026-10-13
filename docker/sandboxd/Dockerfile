# sandboxd 的分发镜像（doc/native-sandbox-design.html §5 / §6.1）。
#
#   docker build -f docker/sandboxd/Dockerfile -t atlas-sandboxd:dev \
#     --build-arg HTTPS_PROXY=http://host.docker.internal:58591 .
#
# 两种用法：
#   init 容器   cp -R /dist/. /opt/atlas/   —— 把守护进程注入沙箱主容器（主容器可以是任意 glibc 镜像）
#   sidecar     /dist/bin/sandboxd barrier  —— 持有 rclone 远程控制口令的屏障（§7.2）
#
# /dist 的内容：
#   python/       独立的 CPython（python-build-standalone，经 uv 安装）—— 不依赖目标镜像有没有 Python
#                 sandboxd 只用标准库，直接放进它的 site-packages
#   bin/sandboxd  启动脚本（POSIX sh）
#   bin/tini      1 号进程：命令里后台进程的孤儿要有人收尸

FROM debian:trixie-slim AS build
ARG TARGETARCH
ARG PYTHON_VERSION=3.13
ARG TINI_VERSION=v0.19.0
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
# uv 经 RUN 里的 curl 下载（走构建参数里的代理）；COPY --from=ghcr.io 是 daemon 去拉，不走构建代理
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh

RUN UV_PYTHON_INSTALL_DIR=/tmp/py uv python install "${PYTHON_VERSION}" \
    && mkdir -p /dist/bin \
    # uv 会同时建一个次版本号的软链接，按真实路径取安装目录
    && PY="$(readlink -f "$(UV_PYTHON_INSTALL_DIR=/tmp/py uv python find --managed-python "${PYTHON_VERSION}")")" \
    && mv "$(dirname "$(dirname "$PY")")" /dist/python \
    # 测试套件、idle、tkinter 用不到（约占一半体积）
    && rm -rf /dist/python/lib/python3*/test /dist/python/lib/python3*/idlelib \
              /dist/python/lib/python3*/tkinter /dist/python/lib/python3*/turtledemo \
    && find /dist/python -name __pycache__ -prune -exec rm -rf {} +

RUN curl -fsSL -o /dist/bin/tini \
        "https://github.com/krallin/tini/releases/download/${TINI_VERSION}/tini-static-${TARGETARCH}" \
    && chmod 0755 /dist/bin/tini

COPY agent-platform/pod/sandboxd/src/atlas_sandboxd /tmp/atlas_sandboxd
RUN SITE="$(/dist/python/bin/python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
    && cp -r /tmp/atlas_sandboxd "$SITE/atlas_sandboxd" \
    && printf '%s\n' \
        '#!/bin/sh' \
        '# 自身路径推出安装目录：/opt/atlas/bin/sandboxd 或 /dist/bin/sandboxd' \
        'D="${0%/bin/sandboxd}"' \
        '# -E：不读目标镜像的 PYTHON* 变量；-s：不加用户 site —— 解释器与目标镜像完全隔离' \
        'exec "$D/python/bin/python3" -E -s -m atlas_sandboxd "$@"' \
        > /dist/bin/sandboxd \
    && chmod 0755 /dist/bin/sandboxd \
    && /dist/python/bin/python3 -E -s -c 'import atlas_sandboxd'

FROM debian:trixie-slim
COPY --from=build /dist /dist
# 非 root 运行（Pod 里以 runAsUser 1000 跑）
USER 1000
ENTRYPOINT ["/dist/bin/sandboxd"]
CMD ["barrier"]
