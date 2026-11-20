"""Atlas 租户隔离（c4-model/infra-platform/code-tenancy.html）。

数据访问层按当前租户自动过滤：业务表继承 TenantOwned，查询自动加租户条件、新行自动填租户；
没有租户上下文就碰租户表直接报错，而不是返回全部租户的数据。规则和用户中心现有的一样。

    from atlas_tenancy import TenantOwned, TenantScope

    class Agent(TenantOwned, Base): ...

    with TenantScope.enter(run.tenant_uuid):
        await session.execute(select(Agent))     # 只会查到这个租户的
"""

from . import filter as _filter  # noqa: F401 —— 导入即挂上会话事件
from .context import SystemReason, TenantScope, TenantViolation
from .helpers import TENANT_HEADER, TenantNaming, TenantSweep, tenant_headers
from .model import TenantOwned
from .purge import PurgeReport, TenantDataPurge
from .web import parse_tenant, tenant_dependency

__all__ = [
    "TENANT_HEADER",
    "PurgeReport",
    "SystemReason",
    "TenantDataPurge",
    "TenantNaming",
    "TenantOwned",
    "TenantScope",
    "TenantSweep",
    "TenantViolation",
    "parse_tenant",
    "tenant_dependency",
    "tenant_headers",
]
