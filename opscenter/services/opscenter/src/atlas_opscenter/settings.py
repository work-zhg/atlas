"""运营中心的设置。

★ 系统登记（有哪些系统、地址、能力）放部署配置，不进库（c4-model/opscenter/code-gateway.html
  · SystemRegistry）；每个系统的运营密钥放 Secret（system_keys），和各系统自己的 ops_api_key 一致。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["OpsSettings", "SystemConfig", "get_settings"]


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    return next((p for p in here.parents if (p / "uv.lock").is_file()), here.parents[3])


_REPO_ENV = _repo_root() / ".env"


class SystemConfig(BaseModel):
    """一个系统的登记项。capabilities 见 gateway.Capability。

    module_code：这个系统在运营中心里那个模块的数据编码（如 OpsMcp）。
    运营中心启动后把它同步进用户中心，
    系统租户的管理员给人授它的「全部」级别，这个人才能运营这个系统（access.py）。
    """

    code: str
    name: str
    base_url: str
    capabilities: list[str]
    module_code: str


#: 本机开发的默认登记：各服务在宿主上的默认端口（部署时用 ATLAS_OPS_SYSTEMS 覆盖）
_LOCAL_SYSTEMS = [
    SystemConfig(
        code="usercenter",
        name="用户中心",
        base_url="http://127.0.0.1:8030",
        capabilities=["user_center"],
        module_code="OpsUserCenter",
    ),
    SystemConfig(
        code="atlas",
        name="Atlas",
        base_url="http://127.0.0.1:8000",
        capabilities=["lifecycle", "capacity", "settings", "jobs", "runtime_env"],
        module_code="OpsAtlas",
    ),
    SystemConfig(
        code="knowledge",
        name="知识库",
        base_url="http://127.0.0.1:8050",
        capabilities=["lifecycle", "capacity", "settings", "jobs"],
        module_code="OpsKnowledge",
    ),
    SystemConfig(
        code="skill",
        name="技能",
        base_url="http://127.0.0.1:8020",
        capabilities=["lifecycle", "skill_market"],
        module_code="OpsSkill",
    ),
    SystemConfig(
        code="mcpgateway",
        name="MCP 网关",
        base_url="http://127.0.0.1:8070",
        capabilities=["lifecycle", "jobs"],
        module_code="OpsMcp",
    ),
    SystemConfig(
        code="modelgateway",
        name="模型平台",
        base_url="http://127.0.0.1:8090",
        capabilities=["lifecycle", "jobs"],
        module_code="OpsModel",
    ),
    SystemConfig(
        code="memory",
        name="记忆服务",
        base_url="http://127.0.0.1:8060",
        capabilities=["lifecycle", "memory_scopes"],
        module_code="OpsMemory",
    ),
]


class OpsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ATLAS_OPS_", env_file=_REPO_ENV, extra="ignore")

    database_url: str = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas_opscenter"
    cors_origins: list[str] = ["http://localhost:3300", "http://127.0.0.1:3300"]

    # ── 运营人员与权限（access.py）：登录在用户中心，请求经 API 网关写上身份头 ────────────
    #: 用户中心的授权快照、各模块级别缓存多久：收回授权、停用账号最长这么久生效
    authz_cache_s: float = 30.0
    #: 「应用切换」「我的应用」点开运营中心去这里（同步进用户中心的应用目录）
    home_url: str = "/opscenter-ui/"
    #: 同步自己的应用目录、给系统租户开通的间隔（秒）
    catalog_sync_s: float = 300.0

    # ── 系统接入 ──────────────────────────────────────────────────────
    #: JSON：[{code, name, base_url, capabilities}]
    systems: list[SystemConfig] = _LOCAL_SYSTEMS
    #: JSON：{系统编码: 运营密钥}（部署 Secret）。没有密钥的系统一律调不通（对方也会 403）
    system_keys: dict[str, str] = {}
    #: 调各系统的超时（秒）。删除一批数据可能要一会儿
    call_timeout_s: float = 60.0

    # ── 后台任务 ──────────────────────────────────────────────────────
    #: 推进租户操作的间隔（秒）
    sweep_interval_s: float = 5.0
    #: 一个实例推进一个操作的租约（秒）：崩了之后别的实例最多等这么久接手
    lease_s: float = 120.0


@lru_cache
def get_settings() -> OpsSettings:
    return OpsSettings()
