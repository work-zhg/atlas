"""本库没有自己的表。

0001 建过两张表（app_tenant_state 本地租户状态、app_ops_request 运营请求记录），0002 删掉了：
业务系统不再存租户状态，创建、删除本身就能重做，不需要按幂等键去重。迁移链留着，
宿主照旧在自己的迁移步骤里调一次 atlas_opsapi.upgrade，存量库里的两张表由它删掉。
"""

from __future__ import annotations

import sqlalchemy as sa

__all__ = ["metadata"]

metadata = sa.MetaData()
