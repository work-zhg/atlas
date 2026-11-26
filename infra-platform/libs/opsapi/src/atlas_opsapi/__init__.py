"""Atlas 运营接口（c4-model/infra-platform/code-opsapi.html）：业务系统接入运营中心的中间件。

通用的部分都在这里：路由挂在 /ops/v1、只认运营中心（签名 + 时间窗）；租户的创建在这个租户的
上下文里执行，删除先外部、后数据库，分批。宿主只继承 TenantLifecycle，写创建和删除。

    class MyLifecycle(TenantLifecycle):
        purge_order = (Child, Parent)              # 本系统的租户表，子表在前

        async def create(self, tenant_uuid, params): …   # 新租户先准备什么

    ops = OpsRoutes.mount(app, OpsGuard(settings.ops_api_key), MyLifecycle(sessions, base=Base),
                          config=app_config, jobs=jobs)

★ 业务系统不知道租户状态：没有停用、恢复，后台工作也不查状态（用户中心对停用、到期租户的人
  一律返回无权限）。
"""

from .guard import OpsGuard
from .lifecycle import Rejected, TenantLifecycle
from .migrate import downgrade, upgrade
from .routes import PREFIX, OpsRoutes
from .signature import SignatureError, sign, verify
from .stamp import OperatorStamp

__all__ = [
    "PREFIX",
    "OperatorStamp",
    "OpsGuard",
    "OpsRoutes",
    "Rejected",
    "SignatureError",
    "TenantLifecycle",
    "downgrade",
    "sign",
    "upgrade",
    "verify",
]
