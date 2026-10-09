"""TeamFlow 的设置。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["TFSettings", "get_settings"]

_REPO_ENV = Path(__file__).resolve().parents[3] / ".env"


class TFSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ATLAS_TF_", env_file=_REPO_ENV, extra="ignore")

    #: TeamFlow 库：同一 PG 实例上的独立 database
    database_url: str = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas_teamflow"
    cors_origins: list[str] = ["http://localhost:3200", "http://127.0.0.1:3200"]
    cookie_secure: bool = False
    session_hours: int = 12

    # ── 用户中心（开放接口）──────────────────────────────────────────
    uc_base_url: str = "http://127.0.0.1:8030"
    uc_app_key: str = ""
    uc_app_secret: SecretStr = SecretStr("")
    #: 授权快照（角色 / 操作码 / 菜单）最长缓存（权限设计 §10）
    snapshot_ttl_seconds: int = 300
    #: 团队级别校验结果的进程内缓存
    team_level_ttl_seconds: int = 30

    # ── Atlas（Agent）────────────────────────────────────────────────
    atlas_base_url: str = "http://127.0.0.1:8000"
    #: 调 Atlas 时使用的服务身份（Atlas 按 X-User-Id 识别调用方，须是 UUID）。
    #: 固定值 = uuid5(DNS, "teamflow.atlas.local")：TeamFlow 建的会话都归在这个身份下
    atlas_user_id: str = "3a08906c-3427-5e0c-b9b4-ae74140bb420"
    #: 团队 Agent 快照的同步间隔
    agent_sync_seconds: int = 600

    # ── 文件模板 ────────────────────────────────────────────────────
    file_template_max_bytes: int = 100 * 1024

    # ── 产物（Git）──────────────────────────────────────────────────
    #: gitee = 经 Gitee API 提交（多实例必须）；local = 本机 git CLI（单实例开发 / 测试）
    git_backend: Literal["local", "gitee"] = "local"
    gitee_api: str = "https://gitee.com/api/v5"
    #: ★ 只放 .env，不进代码与配置文件
    gitee_token: SecretStr = SecretStr("")
    #: 仓库所在的命名空间（个人或组织）；空 = token 本人
    gitee_namespace: str = ""
    gitee_repo_prefix: str = "teamflow-"
    #: local 后端：仓库根目录 {root}/{团队 uuid}/{项目 uuid}
    git_root: Path = Path.home() / ".atlas" / "teamflow" / "repos"
    git_author_name: str = "AI TeamFlow"
    git_author_email: str = "teamflow@atlas.local"

    # ── 多实例协调 ──────────────────────────────────────────────────
    #: 配置后经 Redis 协调（广播、唤醒、节点租约锁）；空 = 单进程内存实现
    redis_url: str | None = None

    @field_validator("redis_url")
    @classmethod
    def _empty_is_none(cls, v: str | None) -> str | None:
        return v or None

    #: API 进程内是否运行 Agent 监督器。多实例下开着也安全（节点租约锁），
    #: 也可以关掉，只由独立的 worker 进程（python -m atlas_teamflow worker）处理
    embedded_worker: bool = True
    #: 节点租约：持有者每 1/3 续租；进程死掉后过期由其他实例接手（不是工作时长上限）
    worker_lease_seconds: int = 30
    #: 定期扫描待处理节点（接管、补漏掉的唤醒）
    worker_sweep_seconds: int = 15


@lru_cache
def get_settings() -> TFSettings:
    return TFSettings()
