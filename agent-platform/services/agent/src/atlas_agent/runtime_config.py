"""运行期可改的业务参数（支线 C：应用配置库 atlas_appconfig）。

c4-model/infra-platform/code-appconfig.html。

    sandbox.quota_per_user              每人同时几个沙箱 / 会话 Pod（原生沙箱与会话 Pod 合计）
    sandbox.image_allow_prefixes        strict 档可用的镜像前缀
    sandbox.bench_image_allow_prefixes  评测档可用的镜像前缀

★ 默认值取部署配置（同名环境变量）：没在运行期改过的，行为和以前一样；管理员经 /v1/configs 改了，
  下一次读就生效（读不缓存），不用重启。
★ 数据库地址、密钥这类启动参数仍在环境变量；配置值是明文，不能放机密。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from atlas_appconfig import AppConfig, ConfigKey

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from .config import Settings

__all__ = ["RuntimeConfig", "RuntimeKeys"]


@dataclass(frozen=True)
class RuntimeKeys:
    sandbox_quota: ConfigKey[int]
    sandbox_images: ConfigKey[list[str]]
    bench_images: ConfigKey[list[str]]

    @classmethod
    def of(cls, settings: Settings) -> RuntimeKeys:
        return cls(
            sandbox_quota=ConfigKey(
                "sandbox.quota_per_user",
                int,
                default=settings.sandbox_quota_per_user,
                description="每人同时几个沙箱 / 会话 Pod（原生沙箱与会话 Pod 合计）",
            ),
            sandbox_images=ConfigKey(
                "sandbox.image_allow_prefixes",
                list[str],
                default=list(settings.sandbox_image_allow_prefixes),
                description="strict 档可用的沙箱镜像前缀（平台默认镜像总是可用）",
            ),
            bench_images=ConfigKey(
                "sandbox.bench_image_allow_prefixes",
                list[str],
                default=list(settings.sandbox_bench_image_allow_prefixes),
                description="评测档可用的沙箱镜像前缀（root、根可写：只该放评测题的镜像）",
            ),
        )

    def all(self) -> list[ConfigKey[Any]]:
        return [self.sandbox_quota, self.sandbox_images, self.bench_images]


class RuntimeConfig:
    """Atlas 的运行期配置：一个 AppConfig 加上 Atlas 声明的 key。构造不做 IO。"""

    def __init__(self, settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.keys = RuntimeKeys.of(settings)
        self.config = AppConfig(sessions, self.keys.all())

    async def sandbox_quota(self) -> int:
        return await self.config.get(self.keys.sandbox_quota)

    async def image_allow_prefixes(self) -> dict[str, list[str]]:
        strict, bench = await self.config.get_many(self.keys.sandbox_images, self.keys.bench_images)
        return {"strict": strict, "bench": bench}
