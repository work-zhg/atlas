"""pytest 全局夹具。

关键问题：get_engine() / get_sessionmaker() 是 lru_cache 的模块级单例，
而 pytest-asyncio 为每个测试函数新建 event loop。asyncpg 连接是绑定 loop 的，
于是第二个测试会复用第一个测试已关闭 loop 上的池化连接 → "Event loop is closed"，
表现为顺序相关的诡异失败（单跑通过、全量跑失败）。

对策：每个测试结束时在 loop 还活着的时候 dispose 引擎，并清掉缓存，
让下一个测试在自己的 loop 上重建。生产代码保持连接池不变。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from atlas_server.config import Settings, get_settings
from atlas_server.db.session import get_engine, get_sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 仓库根 .env 里属于**对象存储**的那些键。
#: 其余键（database_url / redis_url / litellm_key）测试仍然要从 .env 读 ——
#: 那是"连哪台开发机"，不是"被测对象的配置"。
_OBJECT_STORAGE_KEYS = (
    "OSS_ENDPOINT",
    "OSS_BUCKET",
    "OSS_ROOT",
    "OSS_ACCESS_KEY_ID",
    "OSS_ACCESS_KEY_SECRET",
    "OSS_ADDRESSING_STYLE",
    "OSS_REGION",
)


@pytest.fixture(scope="session", autouse=True)
def _object_storage_is_not_inherited_from_the_developer(tmp_path_factory) -> Iterator[None]:
    """★ 测试不继承开发机 .env 里的对象存储配置。

    踩过一次，而且是**沉默**地踩的：给 .env 配上真 GCS 之后，四条本来
    密闭的用例开始连公网 ——

      - `Settings(litellm_key="x")` 明明没给 bucket，却从 .env 拿到一个，
        于是"没配 bucket 就没有文件能力"这条断言反过来了；
      - moto 的 `mock_aws()` 被 .env 里的 `oss_endpoint` 绕开，用例名写着
        内存桶，实际打的是真桶。

    两种都不是产品 bug，是**测试读了不该读的东西**；而且方向很坏 ——
    CI 上没有 .env，所以它只在开发机上挂，或者更糟：只在开发机上悄悄地
    对着真桶跑绿。

    对策是按键过滤，而不是整个关掉 env_file：DB 与 Redis 的地址仍得从
    .env 来。要对着真 GCS 跑的用例显式传 `_env_file` 自己读回来
    （见 tests/test_oss_gcs.py）。
    """
    filtered = tmp_path_factory.mktemp("env") / "env-without-object-storage"
    source = REPO_ROOT / ".env"
    lines = source.read_text(encoding="utf-8").splitlines() if source.exists() else []
    filtered.write_text(
        "\n".join(
            line
            for line in lines
            if line.strip().split("=", 1)[0].strip().upper() not in _OBJECT_STORAGE_KEYS
        ),
        encoding="utf-8",
    )

    saved = {key: os.environ.pop(key, None) for key in _OBJECT_STORAGE_KEYS}
    Settings.model_config["env_file"] = filtered
    get_settings.cache_clear()
    try:
        yield
    finally:
        Settings.model_config["env_file"] = source
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value
        get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _reset_db_singletons() -> AsyncIterator[None]:
    yield
    engine = get_engine()
    await engine.dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture
async def clean_db() -> AsyncIterator[None]:
    """清掉测试造的数据，保留迁移种子（内置 agent / model_catalog / 默认用户）。

    thread 删除会级联带走 message / run / run_file；
    agent 删除会级联带走 agent_version。内置 agent 必须留着 —— 它是产品的一部分。

    ★ 必须先把 current_version_id 置空：agent.current_version_id 指向
      agent_version，而 agent_version.agent_id 是 ON DELETE CASCADE。
      删 agent 时 MySQL 会先看到「父行仍被引用」而拒绝（错误 1451），
      PostgreSQL 则能自行排序。不置空的话 MySQL 上所有 DB 测试全挂在夹具里。
    """
    from sqlalchemy import text

    async def _wipe() -> None:
        async with get_sessionmaker()() as session:
            # ★ 子会话先走：thread.parent_thread_id 是自引用外键，而 MySQL
            #   不对同表的自引用触发级联删除（PG 会），留着的话父行删不掉
            #   （1451）。两步删就够 —— 委派深度结构性封顶在 1。
            await session.execute(text("DELETE FROM thread WHERE parent_thread_id IS NOT NULL"))
            await session.execute(text("DELETE FROM thread"))
            await session.execute(
                text("UPDATE agent SET current_version_id = NULL WHERE is_builtin = false")
            )
            await session.execute(text("DELETE FROM agent WHERE is_builtin = false"))
            await session.commit()

    await _wipe()
    yield
    await _wipe()
