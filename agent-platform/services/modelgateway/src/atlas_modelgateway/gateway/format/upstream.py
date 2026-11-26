"""连上游兼容导出：实际定义已下沉至 shared.upstream，保留此模块供网关内部与测试按原路径导入。"""

from ...shared.upstream import AddressGuard, UpstreamClient, UpstreamError

__all__ = ["AddressGuard", "UpstreamClient", "UpstreamError"]
