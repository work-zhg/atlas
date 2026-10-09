"""用户中心的设置。

★ 安全策略模块本期不做（总体设计 §02）：密码与登录保护先用这里的固定值，
  以后由安全策略模块提供配置界面接管。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["UCSettings", "get_settings"]

_REPO_ENV = Path(__file__).resolve().parents[3] / ".env"


class UCSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ATLAS_UC_", env_file=_REPO_ENV, extra="ignore")

    #: 用户中心库：与运行时、配置服务同一个 PG 实例上的**另一个 database**。
    #: ★ 用 localhost 而不是 127.0.0.1（同 atlas_config 的说明：避开本机 brew 起的 PG）。
    database_url: str = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas_usercenter"
    #: 管理台前端（dev 时 Next 经 rewrites 代理同源访问，CORS 只给直连调试用）
    cors_origins: list[str] = ["http://localhost:3100", "http://127.0.0.1:3100"]
    #: 生产必须 True（HTTPS）；本机 http 开发为 False
    cookie_secure: bool = False

    # ── 密码与登录保护（固定值，见模块说明）──────────────────────────
    password_min_len: int = 8
    password_need_upper: bool = True
    password_need_digit: bool = True
    password_need_symbol: bool = False
    password_expire_days: int = 90
    lock_after_failures: int = 5
    lock_minutes: int = 15
    session_hours: int = 12
    temp_password_days: int = 7

    # ── 开放接口 ─────────────────────────────────────────────────────
    app_token_hours: int = 2


@lru_cache
def get_settings() -> UCSettings:
    return UCSettings()
