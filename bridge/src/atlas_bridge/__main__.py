"""入口：``python -m atlas_bridge``。读配置 → 构造 BridgeApp → 运行。"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from .app import BridgeApp
from .config import BridgeConfig


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("ATLAS_BRIDGE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = BridgeConfig.from_env(os.environ)
    return asyncio.run(BridgeApp(config).run())


if __name__ == "__main__":
    sys.exit(main())
