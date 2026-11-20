"""Atlas 运营接口（c4-model/infra-platform/code-opsapi.html）：业务服务实现运营中心要求的契约。

通用的部分都在这里：路由挂在 /ops/v1、只认运营中心（签名 + 时间窗）、在系统范围里执行、
按幂等键去重并记下每个运营请求、本地记一份租户状态给后台工作查。宿主只实现 TenantLifecycle。

    gate = TenantGate(sessions)
    lifecycle = TenantLifecycleService(sessions, MyHooks(), gate, TenantDataPurge([...子表在前...]))
    ops = OpsRoutes(OpsGuard(settings.ops_api_key), lifecycle, config=app_config, jobs=jobs)
    app.include_router(ops.router)

    # 后台工作开始前
    await gate.require_active(run.tenant_uuid)
"""

from .guard import OpsGuard
from .lifecycle import NoExternal, OpsRequestLedger, TenantLifecycle, TenantLifecycleService
from .migrate import downgrade, upgrade
from .routes import PREFIX, OpsRoutes
from .signature import SignatureError, sign, verify
from .stamp import OperatorStamp
from .state import Rejected, TenantGate, TenantState, TenantStatus

__all__ = [
    "PREFIX",
    "NoExternal",
    "OperatorStamp",
    "OpsGuard",
    "OpsRequestLedger",
    "OpsRoutes",
    "Rejected",
    "SignatureError",
    "TenantGate",
    "TenantLifecycle",
    "TenantLifecycleService",
    "TenantState",
    "TenantStatus",
    "downgrade",
    "sign",
    "upgrade",
    "verify",
]
