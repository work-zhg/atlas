"""computer use：看屏幕，用系统的键盘鼠标（c4-model/agent/browser/code-browser-screen.html）。

    ScreenDesk      子命令的入口：look · tap · double-tap · drag · scroll · key · paste ·
                    copy-page · go
    ScreenAction    一个键鼠动作（坐标是截图上的坐标）
    CoordinateMap   截图坐标 → 屏幕坐标：乘缩放比例、加上这一块的左边界；超出这一块就拒绝
    InputQueue      一台电脑（一个沙箱）一把本机锁：一次只执行一个会话的一个键鼠动作
    BlockCapture    只截这个对话那一块（1920×1080），缩到 1280 宽存成 PNG
    XInput          系统层面的键鼠（xdotool）

★ 完全不经过调试接口：网页收到的是 X 服务器送来的普通键鼠事件，分不出是人还是程序。窗口置前用
  浏览器级的调试命令（rawcdp.WindowOps），不附着页面。
★ 文字输入走剪贴板粘贴（逐键模拟输不了中文）；读文字是全选、复制、从剪贴板取整页纯文本。
  剪贴板和人接管时共用（clipboard.py）。
★ 每个动作做完等页面动一下，自动截一张新图：模型下一步就看它，确认点中了没有。
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .clipboard import read_text, write_text
from .paths import DISPLAY, Paths
from .screens import SLOT_H, SLOT_W, Slot

__all__ = [
    "COMMANDS",
    "BlockCapture",
    "CoordinateMap",
    "InputQueue",
    "OutOfBlock",
    "ScreenAction",
    "ScreenDesk",
    "Shot",
    "XInput",
]

#: 交给模型的截图宽度：整块 1920 宽太费 token，1280 宽认字、找按钮够用
SHOT_W = 1280
SHOT_H = SLOT_H * SHOT_W // SLOT_W
#: 动作之后等页面动一下再截图
SETTLE_S = 0.8
#: 打开网址之后多等一会儿
NAVIGATE_SETTLE_S = 2.5
#: 地址栏获得焦点、粘贴生效（向剪贴板的主人要内容是异步的）各等多久
KEY_GAP_S = 0.2
PASTE_SETTLE_S = 0.5
#: computer use 的子命令（其余子命令走快照，交给 Playwright CLI）
COMMANDS = frozenset(
    {"look", "tap", "double-tap", "drag", "scroll", "key", "paste", "copy-page", "go"}
)
#: 模型常写的键名 → xdotool 的键名
_KEY_ALIAS = {
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "backspace": "BackSpace",
    "delete": "Delete",
    "space": "space",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "arrowup": "Up",
    "arrowdown": "Down",
    "arrowleft": "Left",
    "arrowright": "Right",
    "pageup": "Prior",
    "pagedown": "Next",
    "home": "Home",
    "end": "End",
    "control": "ctrl",
    "cmd": "ctrl",
    "meta": "ctrl",
    "command": "ctrl",
}


class OutOfBlock(ValueError):
    """坐标不在这个对话那一块里：点不到别的对话的窗口。"""


# ---------------------------------------------------------------------------- 坐标


@dataclass(frozen=True)
class CoordinateMap:
    slot: Slot
    shot_w: int = SHOT_W
    shot_h: int = SHOT_H

    @property
    def scale(self) -> float:
        return SLOT_W / self.shot_w

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        """截图坐标 → 屏幕坐标。截图外（也就是这一块外）的拒绝。"""
        if not (0 <= x < self.shot_w and 0 <= y < self.shot_h):
            raise OutOfBlock(
                f"坐标 ({x:g}, {y:g}) 不在截图里（宽 {self.shot_w}、高 {self.shot_h}）："
                "按最近一张截图报坐标"
            )
        return self.slot.left + round(x * self.scale), round(y * self.scale)


# ---------------------------------------------------------------------------- 动作


@dataclass(frozen=True)
class ScreenAction:
    kind: Literal["tap", "double-tap", "drag", "scroll", "key", "paste", "go"]
    at: tuple[float, float] | None = None
    to: tuple[float, float] | None = None
    #: scroll：up / down / left / right；key：组合键；paste、go：文字或网址
    text: str = ""
    times: int = 1

    @classmethod
    def parse(cls, args: list[str]) -> ScreenAction:
        """命令行参数 → 动作。不合法抛 ValueError（消息给模型看）。"""
        name, rest = args[0], args[1:]
        if name in ("tap", "double-tap"):
            x, y = _numbers(rest, 2, f"browser {name} <x> <y>")
            return cls(name, at=(x, y))  # type: ignore[arg-type]
        if name == "drag":
            x1, y1, x2, y2 = _numbers(rest, 4, "browser drag <x1> <y1> <x2> <y2>")
            return cls("drag", at=(x1, y1), to=(x2, y2))
        if name == "scroll":
            usage = "browser scroll <x> <y> <up|down|left|right> [次数]"
            if len(rest) not in (3, 4) or rest[2] not in ("up", "down", "left", "right"):
                raise ValueError(f"用法：{usage}")
            x, y = _numbers(rest[:2], 2, usage)
            times = int(rest[3]) if len(rest) == 4 and rest[3].isdigit() else 5
            return cls("scroll", at=(x, y), text=rest[2], times=max(1, min(times, 30)))
        if name == "key":
            if len(rest) != 1 or not rest[0].strip():
                raise ValueError("用法：browser key <键>（如 Enter、Tab、ctrl+a）")
            return cls("key", text=_key(rest[0]))
        if name in ("paste", "go"):
            text = " ".join(rest)
            if not text:
                usage = "browser paste <文字>" if name == "paste" else "browser go <网址>"
                raise ValueError(f"用法：{usage}")
            return cls(name, text=text)  # type: ignore[arg-type]
        raise ValueError(f"不认识的动作：{name}")


def _numbers(raw: list[str], n: int, usage: str) -> list[float]:
    if len(raw) != n:
        raise ValueError(f"用法：{usage}")
    try:
        return [float(v.strip("(),")) for v in raw]
    except ValueError as exc:
        raise ValueError(f"坐标要是数字。用法：{usage}") from exc


def _key(raw: str) -> str:
    parts = [p.strip() for p in raw.replace(" ", "").split("+") if p.strip()]
    return "+".join(_KEY_ALIAS.get(p.lower(), p) for p in parts)


# ---------------------------------------------------------------------------- 系统键鼠


class XInput:
    """xdotool：事件经 X 服务器进浏览器，和人在画面里操作一样。"""

    def __init__(self, display: str = DISPLAY) -> None:
        self._env = {**os.environ, "DISPLAY": display}

    def click(self, x: int, y: int, *, repeat: int = 1) -> None:
        self._run("mousemove", "--sync", str(x), str(y))
        self._run("click", "--repeat", str(repeat), "--delay", "80", "1")

    def drag(self, a: tuple[int, int], b: tuple[int, int], *, steps: int = 12) -> None:
        """按下、分几步挪过去、松开：滑块要看到中间的移动才认。"""
        self._run("mousemove", "--sync", str(a[0]), str(a[1]))
        self._run("mousedown", "1")
        for i in range(1, steps + 1):
            x = a[0] + (b[0] - a[0]) * i // steps
            y = a[1] + (b[1] - a[1]) * i // steps
            self._run("mousemove", "--sync", str(x), str(y))
            time.sleep(0.02)
        self._run("mouseup", "1")

    def scroll(self, x: int, y: int, direction: str, times: int) -> None:
        button = {"up": "4", "down": "5", "left": "6", "right": "7"}[direction]
        self._run("mousemove", "--sync", str(x), str(y))
        self._run("click", "--repeat", str(times), "--delay", "50", button)

    def keys(self, combo: str) -> None:
        self._run("key", "--clearmodifiers", combo)

    def _run(self, *args: str) -> None:
        done = subprocess.run(
            ["xdotool", *args], env=self._env, capture_output=True, text=True, timeout=10
        )
        if done.returncode != 0:
            raise RuntimeError(f"xdotool {args[0]} 失败：{done.stderr.strip() or done.returncode}")


# ---------------------------------------------------------------------------- 排队


class InputQueue:
    """一个屏幕只有一个键盘焦点和一个剪贴板：两个对话同时打字会打进对方窗口。

    ★ 文件锁：几个会话的 browser 命令是不同的进程。每个动作很短，排队几乎感觉不到。
    """

    def __init__(self, paths: Paths) -> None:
        self._paths = paths

    @contextmanager
    def turn(self) -> Iterator[None]:
        self._paths.ensure()
        with self._paths.input_lock.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield


# ---------------------------------------------------------------------------- 截图


@dataclass(frozen=True)
class Shot:
    path: Path
    width: int
    height: int


class BlockCapture:
    """只截这一块：同一块里的接管窗口、弹出的窗口也截到，和人看到的画面一致；看不到别的对话。"""

    def __init__(self, display: str = DISPLAY) -> None:
        self._display = display

    def capture(self, slot: Slot, out: Path) -> Shot:
        from PIL import ImageGrab

        box = (slot.left, 0, slot.left + SLOT_W, SLOT_H)
        image = ImageGrab.grab(bbox=box, xdisplay=self._display)
        image = image.convert("RGB").resize((SHOT_W, SHOT_H))
        out.parent.mkdir(parents=True, exist_ok=True)
        image.save(out, format="PNG", optimize=True)
        return Shot(out, SHOT_W, SHOT_H)


# ---------------------------------------------------------------------------- 入口


class ScreenDesk:
    """一个会话的 computer use。target = 这个会话的窗口，slot = 对话那一块。"""

    def __init__(
        self,
        paths: Paths,
        *,
        target: str,
        slot: Slot,
        out: Path,
        xinput: XInput | None = None,
        capture: BlockCapture | None = None,
    ) -> None:
        self._paths = paths
        self._target = target
        self._slot = slot
        self._out = out
        self._map = CoordinateMap(slot)
        self._x = xinput or XInput()
        self._capture = capture or BlockCapture()
        self._queue = InputQueue(paths)

    def run(self, args: list[str]) -> str:
        """执行一个子命令，返回给模型看的输出。参数不对抛 ValueError。"""
        name = args[0]
        if name == "look":
            return self._look_text(self.look())
        if name == "copy-page":
            return self.copy_page()
        action = ScreenAction.parse(args)
        shot = self.act(action)
        return f"{_done(action)}\n{self._look_text(shot)}"

    # ------------------------------------------------------------------ 看

    def look(self) -> Shot:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return self._capture.capture(self._slot, self._out / f"look-{stamp}-{os.getpid()}.png")

    def _look_text(self, shot: Shot) -> str:
        return (
            f"截图：{shot.path}（{shot.width}×{shot.height}，坐标按这张图报；用读文件的工具看它）"
        )

    # ------------------------------------------------------------------ 动

    def act(self, action: ScreenAction) -> Shot:
        """排队、置前、换算坐标后执行，等页面动一下再截一张。越界抛 OutOfBlock（不执行）。"""
        at = self._map.to_screen(*action.at) if action.at is not None else None
        to = self._map.to_screen(*action.to) if action.to is not None else None
        with self._queue.turn():
            self._front()
            match action.kind:
                case "tap":
                    assert at is not None
                    self._x.click(*at)
                case "double-tap":
                    assert at is not None
                    self._x.click(*at, repeat=2)
                case "drag":
                    assert at is not None and to is not None
                    self._x.drag(at, to)
                case "scroll":
                    assert at is not None
                    self._x.scroll(*at, action.text, action.times)
                case "key":
                    self._x.keys(action.text)
                case "paste":
                    write_text(action.text)
                    self._x.keys("ctrl+v")
                case "go":
                    # 在自己窗口的地址栏里输入网址（经剪贴板，网址里有中文也行；不逐字输入：地址栏会
                    # 自动补全成访问过的网址）、回车。
                    # ★ 每步之间等一下：粘贴是 Chrome 再去向剪贴板的主人要内容，是异步的 —— 回车
                    #   紧跟着按，会先于粘贴生效（11-16 VPS 实测：网址接在了旧地址后面、没打开）
                    write_text(action.text)
                    self._x.keys("ctrl+l")
                    time.sleep(KEY_GAP_S)
                    self._x.keys("ctrl+a")
                    self._x.keys("ctrl+v")
                    time.sleep(PASTE_SETTLE_S)
                    self._x.keys("Return")
            time.sleep(NAVIGATE_SETTLE_S if action.kind == "go" else SETTLE_S)
            return self.look()

    def copy_page(self) -> str:
        """全选、复制、从剪贴板取整页纯文本（没有链接和页面结构）。"""
        with self._queue.turn():
            self._front()
            write_text("")
            self._x.keys("ctrl+a")
            self._x.keys("ctrl+c")
            time.sleep(0.3)
            text = read_text()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = self._out / f"page-{stamp}-{os.getpid()}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if not text.strip():
            return (
                f"没有复制到文字（写在 {path}）：焦点可能在输入框里，"
                "先点一下页面空白处再 copy-page；或者看截图"
            )
        head = text if len(text) <= 8000 else text[:8000] + "\n……（后面还有，读文件看全文）"
        return f"整页文字 {len(text)} 字，全文在 {path}：\n{head}"

    def _front(self) -> None:
        """把这个会话的窗口置前（浏览器级调试命令，不碰页面），键盘才落在它上面。"""
        from .rawcdp import browser_session

        async def front() -> None:
            async with browser_session() as windows:
                await windows.activate(self._target)

        asyncio.run(front())
        time.sleep(0.15)


def _done(action: ScreenAction) -> str:
    match action.kind:
        case "tap":
            return f"已点击 {_xy(action.at)}。"
        case "double-tap":
            return f"已双击 {_xy(action.at)}。"
        case "drag":
            return f"已从 {_xy(action.at)} 拖到 {_xy(action.to)}。"
        case "scroll":
            return f"已在 {_xy(action.at)} 向{_DIR[action.text]}滚动 {action.times} 格。"
        case "key":
            return f"已按 {action.text}。"
        case "paste":
            return f"已粘贴 {len(action.text)} 个字。"
        case "go":
            return f"已在地址栏打开 {action.text}。"
    return "已执行。"


_DIR = {"up": "上", "down": "下", "left": "左", "right": "右"}


def _xy(at: tuple[float, float] | None) -> str:
    return f"({at[0]:g}, {at[1]:g})" if at is not None else "?"
