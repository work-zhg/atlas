"""Atlas 连接器：装在用户电脑上，平台在这台电脑上的一切都经过它。

    link      控制连接：配对、连上设备网关并保持、按通道分发
    session   本机会话：本机策略、拉起 bridge、接上会话流
    files     本机文件：路径把关、列改动、列目录、列允许目录、取文件（只读）
    control   本机控制：命令行查看、结束会话

整体设计见 c4-model/agent/device/design-device-cli.html。
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("atlas-connect")
except PackageNotFoundError:  # 源码直接运行
    __version__ = "1.0.0"
