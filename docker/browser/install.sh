#!/bin/sh
# 浏览器镜像的公共安装步骤（c4-model/agent/browser/design-browser.html），以 root 在构建时执行。
# 两个镜像共用：浏览器沙箱镜像（docker/browser/Dockerfile.sandbox，原生智能体）、
# 浏览器 CLI 镜像（docker/browser/Dockerfile.cli，CLI 智能体的会话 Pod）。
#
#   Chromium + 中文字体     有界面模式，画在虚拟屏幕上（人接管时看到的就是它）
#   Xvfb · x11vnc · websockify   虚拟屏幕和画面服务：VNC 只听本机，websockify 对外开 6080（带口令）
#   xclip                   虚拟屏幕的剪贴板：人在画面里复制粘贴经它（VNC 的剪贴板只认 Latin-1）
#   xdotool                 computer use 的系统键鼠（不经过调试接口，网站认不出）
#   websockets · pillow     浏览器级调试连接（窗口、登录态进出）；computer use 截屏
#   @playwright/cli         模型的 browser 命令把它当后端（按会话名各用一个窗口）
#   atlas-browserd          守护和 browser 命令（agent-platform/pod/browserd）
#
# ★ 都在构建时装好：运行时现装太慢（Chromium 加字体几百 MB）。
# ★ 版本钉死：浏览器能力的行为（会话怎么连、快照的格式）跟着这几个版本走，换版本 = 换镜像 tag。
set -eu

WEBSOCKETS_VERSION="${WEBSOCKETS_VERSION:-15.0.1}"
PILLOW_VERSION="${PILLOW_VERSION:-11.3.0}"
PLAYWRIGHT_CLI_VERSION="${PLAYWRIGHT_CLI_VERSION:-0.1.24}"
SRC="${BROWSERD_SRC:-/tmp/browserd}"

# 分三步、三层：变得最少的在前。下载慢、半路断了重来时，已经装好的层不用再下一遍
# （apt 这一步走开发机代理要一个多小时）
case "${1:-all}" in
  system)
    apt-get update
    apt-get install -y --no-install-recommends \
        chromium \
        fonts-noto-cjk fonts-noto-color-emoji \
        xvfb x11vnc websockify xclip xdotool \
        ca-certificates
    rm -rf /var/lib/apt/lists/*
    ;;
  tools)
    # ★ PIP_USER=0：默认沙箱镜像为模型设了 PIP_USER=1（装到 HOME），构建时要装进系统
    PIP_USER=0 PIP_BREAK_SYSTEM_PACKAGES=1 python3 -m pip install --retries 10 --timeout 120 \
        "websockets==${WEBSOCKETS_VERSION}" "pillow==${PILLOW_VERSION}"
    # ★ 前缀指定到 /usr/local：默认沙箱镜像为模型设的 NPM_CONFIG_PREFIX 在 HOME 下（运行时是
    #   emptyDir，构建时装进去的会被盖住）
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 NPM_CONFIG_PREFIX=/usr/local \
        npm install --global --no-audit --no-fund "@playwright/cli@${PLAYWRIGHT_CLI_VERSION}"
    npm cache clean --force >/dev/null 2>&1 || true
    ;;
  browserd)
    PIP_USER=0 PIP_BREAK_SYSTEM_PACKAGES=1 python3 -m pip install --no-deps --no-cache-dir "$SRC"
    rm -rf "$SRC"
    # 自检：命令都在，Chromium 能报版本
    command -v atlas-browserd browser playwright-cli Xvfb x11vnc websockify xclip xdotool chromium
    chromium --version
    python3 -c "from PIL import features; assert features.check('xcb'), 'Pillow 没有 XCB：截不了屏'"
    ;;
  *)
    echo "用法：install.sh system | tools | browserd" >&2
    exit 2
    ;;
esac
