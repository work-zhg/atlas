"""沙箱守护进程（doc/native-sandbox-design.html §6）。

serve     在沙箱主容器里执行命令（server 经 HTTP 调用）
barrier   在 sidecar 里持有 rclone 远程控制口令，只暴露「刷新缓存 / 查写回队列」（§7.2）
"""

__version__ = "0.1.0"
