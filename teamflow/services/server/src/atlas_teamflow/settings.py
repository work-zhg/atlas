"""TeamFlow 的设置。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["TFSettings", "get_settings"]


def _repo_root() -> Path:
    """仓库根：往上第一个有 uv.lock 的目录 —— 目录层级调整后不用改。
    装进镜像（site-packages）后找不到，退回原来的 parents[3]，行为不变。"""
    here = Path(__file__).resolve()
    return next((p for p in here.parents if (p / "uv.lock").is_file()), here.parents[3])


_REPO_ENV = _repo_root() / ".env"


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
    #: Atlas 服务账号密钥（Atlas 权限设计 §10）：Atlas 开启登录（AUTH_MODE=uc）后必填；
    #: 配了就用它（Authorization: Bearer），atlas_user_id 只在 Atlas 的 dev 模式下生效。只放 .env
    atlas_api_key: SecretStr = SecretStr("")
    #: 团队 Agent 快照的同步间隔
    agent_sync_seconds: int = 600

    # ── 文件模板 ────────────────────────────────────────────────────
    file_template_max_bytes: int = 100 * 1024

    # ── 产物仓库（Gitee）──────────────────────────────────────────
    # ★ TeamFlow 不写仓库：产物由 Agent 在会话 Pod 里用 git CLI 提交（产物仓库与 Agent 提交 · §6）。
    gitee_api: str = "https://gitee.com/api/v5"
    #: 仓库管理令牌：建仓库、读提交、管理协作者。★ 只放 .env，不进代码与配置文件
    gitee_token: SecretStr = SecretStr("")
    #: 仓库所在的命名空间（个人或组织）；空 = 令牌本人
    gitee_namespace: str = ""
    gitee_repo_prefix: str = "teamflow-"
    #: 节点会话使用的 Atlas git 凭据档案（atlas-cluster 的 git_profiles 里的名字）：
    #: Pod 内的 git CLI 用它 push。档案里放的是 Agent 写入用的机器人账号凭据。
    atlas_git_profile: str = "teamflow-gitee"
    #: 成员绑定 Gitee 账号（OAuth，只为证明账号归属，scope=user_info）
    gitee_oauth_client_id: str = ""
    gitee_oauth_client_secret: SecretStr = SecretStr("")
    #: OAuth 回调地址（前端页面 /account/gitee，须与 Gitee 应用里登记的一致）
    gitee_oauth_redirect_uri: str = "http://localhost:3200/account/gitee"
    #: 仓库协作者对账（以团队成员身份为准修正多余 / 缺失）
    collaborator_reconcile_seconds: int = 86400

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
