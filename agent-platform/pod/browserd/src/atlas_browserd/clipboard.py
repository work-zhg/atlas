"""Clipboard：虚拟屏幕的剪贴板（X 的 CLIPBOARD 选区），用 xclip 读写。

人在画面里粘贴：Atlas 先把文字写到这里，画面再给浏览器按 Ctrl+V；复制：画面先按 Ctrl+C，
Atlas 再从这里读。地址栏、网页里的输入框都一样能用。

★ 不走 VNC 协议的剪贴板：x11vnc 只认 Latin-1，中文会变成问号。
★ 内容经文件进出（Atlas 传上来、取走），不进命令行和命令输出：粘贴的常常是密码、验证码。
"""

from __future__ import annotations

import os
import subprocess

from .paths import DISPLAY

__all__ = ["read_text", "write_text"]

_XCLIP = ["xclip", "-selection", "clipboard"]
_TIMEOUT_S = 5


def _env() -> dict[str, str]:
    return {**os.environ, "DISPLAY": DISPLAY}


def write_text(text: str) -> None:
    # ★ xclip -i 读完输入后转到后台、一直持有剪贴板，直到别的程序接手：输出不能接管道，
    #   否则这里要等它退出才拿得到 EOF
    subprocess.run(
        [*_XCLIP, "-i"],
        input=text.encode(),
        env=_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=_TIMEOUT_S,
        check=True,
    )


def read_text() -> str:
    """没有程序持有剪贴板（还没复制过）时是空串。"""
    done = subprocess.run(
        [*_XCLIP, "-o"], env=_env(), capture_output=True, timeout=_TIMEOUT_S, check=False
    )
    return done.stdout.decode(errors="replace") if done.returncode == 0 else ""
