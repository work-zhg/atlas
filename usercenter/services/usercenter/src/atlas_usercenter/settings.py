"""用户中心的设置。

★ 安全策略模块本期不做（总体设计 §02）：密码与登录保护先用这里的固定值，
  以后由安全策略模块提供配置界面接管。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["UCSettings", "get_settings"]


def _repo_root() -> Path:
    """仓库根：往上第一个有 uv.lock 的目录 —— 目录层级调整后不用改。
    装进镜像（site-packages）后找不到，退回原来的 parents[3]，行为不变。"""
    here = Path(__file__).resolve()
    return next((p for p in here.parents if (p / "uv.lock").is_file()), here.parents[3])


_REPO_ENV = _repo_root() / ".env"


class UCSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ATLAS_UC_", env_file=_REPO_ENV, extra="ignore")

    #: 用户中心库：与运行时、配置服务同一个 PG 实例上的**另一个 database**。
    #: ★ 用 localhost 而不是 127.0.0.1（同 atlas_skill 的说明：避开本机 brew 起的 PG）。
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
    temp_password_days: int = 7
    #: 网关校验（/api/v1/auth/verify，旧网关 auth_request 用）的进程内缓存：
    #: 登出 / 停用最多滞后这么久
    verify_cache_ttl_s: float = 5.0

    # ── 登录令牌（c4-model/apigateway/code-auth.html）────────────────────────
    #: 访问 JWT：Ed25519，网关本地验签。15 分钟：登出、停用在网关最多滞后这么久
    #: （下游取授权快照时已拒绝）
    access_minutes: int = 15
    #: 刷新令牌：存 uc_session（只存 SHA-256），从登录起算的绝对期限，轮换不续期
    refresh_days: int = 14
    #: 轮换后旧刷新令牌还能用多久（两个标签页同时刷新、弱网重传）；过了这段再用 = 被盗，整个会话作废
    refresh_grace_s: int = 30
    jwt_issuer: str = "atlas-usercenter"
    jwt_audience: str = "atlas-gateway"
    #: 签名私钥（PKCS#8 PEM，Ed25519；部署 Secret）。空 = 每个进程临时生成一把：只适合单副本开发，
    #: 重启后旧的访问令牌全部失效（前端会自动刷新）
    jwt_signing_key: str = ""
    #: 另外发布的公钥（JSON：{"kid": "公钥 PEM"}）。
    #: 换钥：先把新公钥放这里、过 5 分钟（网关刷新周期）
    #: 再换签名私钥，旧公钥挪到这里、再过 15 分钟删掉
    jwt_published_keys: str = ""
    #: 用户中心接口的外部前缀（经网关是 /uc）：刷新令牌的 Cookie 只发给 <前缀>/api/v1/auth
    cookie_path_prefix: str = "/uc"

    # ── 开放接口 ─────────────────────────────────────────────────────
    app_token_hours: int = 2

    # ── 运营接口（/ops/v1，只给运营中心）─────────────────────────────
    #: 运营中心给本系统签名用的运营密钥（部署 Secret）。空 = 运营接口没启用，一律 403
    ops_api_key: str = ""


@lru_cache
def get_settings() -> UCSettings:
    return UCSettings()
