"""供应商：各家能写成数据的部分（spec）和方言（dialect），网关和管理平台共用。

c4-model/modelgateway/code-gateway-provider.html
"""

from .catalog import ProviderCatalog
from .dialect import Op, ProviderDialect, StandardDialect, join_url
from .spec import PROVIDERS, AuthStyle, ProviderSpec

__all__ = [
    "PROVIDERS",
    "AuthStyle",
    "Op",
    "ProviderCatalog",
    "ProviderDialect",
    "ProviderSpec",
    "StandardDialect",
    "join_url",
]
