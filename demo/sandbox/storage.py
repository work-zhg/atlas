"""对象存储、rclone FUSE 挂载器、barrier sidecar（doc/native-sandbox-design.html §7）。

    文件工具 ── 对象存储 API ──▶ ObjectStorage   {user}/{workspace_thread}/workspace/…
    命令     ── 本地文件系统 ──▶ /workspace（FuseMount 的本地目录）── 缓存 / 写回 ──▶ 同一个前缀

两条路看到的是同一份数据，中间隔着挂载器的两个缓存窗口：

    读  命令读到的是挂载器缓存里的版本。文件工具在对象存储上改了，挂载器不知道 ——
        真实 rclone 过了 dir-cache-time（5 秒）才会去看，窗口内读到的甚至是「新内容按旧长度截断」
        的错数据（§7.1 实测）。demo 简化成：没有 forget 就一直是旧的
    写  命令写的文件先进写回队列，--vfs-write-back（1 秒）之后才上传，之前文件工具读不到

所以 sandboxd 在每条命令的边界上加屏障：执行前 forget，执行后等写回队列清空（sandboxd.py）。

★ 挂载器的远程控制（rc）能调 operations/*、config/*，等于整桶凭据。所以 rc 要口令，口令只给
  挂载器和 barrier 两个 sidecar；sandboxd 和命令所在的主容器拿不到（§7.2）。
  demo 里「容器边界」就是对象引用：sandboxd 手里只有 Barrier，没有 FuseMount，也没有口令。

模拟方式：挂载点是一个真实的本地目录（命令就在里面跑），挂载器的线程定时比对它和对象存储。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from tracing import say

WRITE_BACK_S = 0.3  # 本地改动多久后上传。rclone 是 --vfs-write-back 1s
UPLOAD_S = 0.05  # 上传一个文件的耗时
SCAN_S = 0.05  # 挂载器多久看一次本地改动（真实 rclone 在 FUSE 的写调用里直接知道）


class ObjectStorage:
    """对象存储（内存模拟）：所有 Pod、所有 server 实例共用，Pod 删了它还在。"""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put(self, key: str, data: bytes) -> None:
        with self._lock:
            self._objects[key] = data

    def get(self, key: str) -> bytes | None:
        with self._lock:
            return self._objects.get(key)

    def delete(self, key: str) -> None:
        with self._lock:
            self._objects.pop(key, None)

    def under(self, prefix: str) -> dict[str, bytes]:
        """前缀下的全部对象，键是相对路径。"""
        with self._lock:
            return {k[len(prefix) :]: v for k, v in self._objects.items() if k.startswith(prefix)}


class FuseMount:
    """rclone mount（oss-workspace 容器）。本地目录 ↔ 对象存储前缀，带读缓存和写回队列。"""

    def __init__(self, storage: ObjectStorage, prefix: str, local: Path, rc_pass: str) -> None:
        self._storage = storage
        self._prefix = prefix
        self.local = local
        self._rc_pass = rc_pass
        self._synced: dict[str, bytes] = {}  # 相对路径 → 最后一次和对象存储一致时的内容
        self._dirty: dict[str, float] = {}  # 写回队列：相对路径 → 第一次发现改动的时刻
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._pull()  # 挂载时列一遍
        threading.Thread(target=self._uploader, name="rclone-upload", daemon=True).start()

    def rc(self, password: str, command: str) -> list[str]:
        """远程控制。要口令：这个口理论上能调 operations/* 读写整桶，demo 只实现屏障要的两个。"""
        if password != self._rc_pass:
            raise PermissionError("rclone rc 401：口令不对")
        if command == "vfs/forget":
            return self._pull()
        if command == "vfs/queue":
            with self._lock:
                self._scan()
                return list(self._dirty)
        raise ValueError(f"demo 没有实现 rc 命令 {command}")

    def unmount(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ 内部

    def _local_files(self) -> dict[str, bytes]:
        files = {}
        for path in self.local.rglob("*"):
            if path.is_file():
                try:
                    files[path.relative_to(self.local).as_posix()] = path.read_bytes()
                except OSError:  # 命令正在删它
                    continue
        return files

    def _scan(self) -> None:
        """比对本地目录和上次同步的内容，有改动的进写回队列。调用方持锁。"""
        local, now = self._local_files(), time.monotonic()
        for rel, data in local.items():
            if self._synced.get(rel) != data:
                self._dirty.setdefault(rel, now)
            else:
                self._dirty.pop(rel, None)  # 又改回去了
        for rel in self._synced:
            if rel not in local:
                self._dirty.setdefault(rel, now)  # 命令删了它

    def _pull(self) -> list[str]:
        """vfs/forget：丢掉缓存，按对象存储的现状刷新本地。返回刷新了哪些路径。"""
        with self._lock:
            self._scan()
            remote = self._storage.under(self._prefix)
            changed = []
            for rel, data in remote.items():
                if rel in self._dirty:
                    continue  # 本地有还没上传的改动：不覆盖
                if self._synced.get(rel) != data:
                    path = self.local / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                    self._synced[rel] = data
                    changed.append(rel)
            for rel in [r for r in self._synced if r not in remote and r not in self._dirty]:
                (self.local / rel).unlink(missing_ok=True)  # 文件工具删了它
                del self._synced[rel]
                changed.append(rel)
            return changed

    def _uploader(self) -> None:
        while not self._stop.wait(SCAN_S):
            with self._lock:
                self._scan()
                now = time.monotonic()
                due = [rel for rel, t in self._dirty.items() if now - t >= WRITE_BACK_S]
            for rel in due:
                time.sleep(UPLOAD_S)
                with self._lock:
                    if rel not in self._dirty:
                        continue
                    path = self.local / rel
                    if path.is_file():
                        data = path.read_bytes()
                        self._storage.put(self._prefix + rel, data)
                        self._synced[rel] = data
                    else:
                        self._storage.delete(self._prefix + rel)
                        self._synced.pop(rel, None)
                    del self._dirty[rel]


class Barrier:
    """barrier sidecar：和挂载器拿同一个 rc 口令，对外只有两个无害的操作。

    命令就算直接连上它，也只能让缓存失效、看队列长度。真实的它监听 127.0.0.1:5573。
    """

    def __init__(self, mount: FuseMount, rc_pass: str) -> None:
        self._mount = mount
        self._pass = rc_pass

    def forget(self) -> None:
        changed = self._mount.rc(self._pass, "vfs/forget")
        if changed:
            say("barrier", f"forget → 按对象存储刷新了 {', '.join(sorted(changed))}")

    def queue(self) -> int:
        return len(self._mount.rc(self._pass, "vfs/queue"))


def tree(root: Path) -> str:
    """列一个目录（打印用）。"""
    if not root.exists():
        return "（目录已不存在）"
    names = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    return "、".join(names) or "（空）"


def ensure_dir(path: Path) -> Path:
    os.makedirs(path, exist_ok=True)
    return path
