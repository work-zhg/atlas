"""运行期可改的业务参数（支线 C：应用配置库 atlas_appconfig）。

c4-model/infra-platform/code-appconfig.html。

    sandbox.quota_per_user              每人同时几个沙箱 / 会话 Pod（原生沙箱与会话 Pod 合计）
    sandbox.image_allow_prefixes        strict 档可用的镜像前缀
    sandbox.bench_image_allow_prefixes  评测档可用的镜像前缀
    platform.default_agent              首页输入框默认用的智能体（空 = 没有默认）
    workspace.upload_max_mb             单个文件上传的上限（工作空间、对话里添加文件）
    workspace.quota_mb                  每人工作空间的存储配额（0 = 不限）
    device.preview_max_mb               网页上预览本机文件的大小上限
    device.download_max_mb              网页上下载本机文件的大小上限

★ 默认值取部署配置（同名环境变量）：没在运行期改过的，行为和以前一样；管理员经 /v1/configs 改了，
  下一次读就生效（读不缓存），不用重启。
★ 数据库地址、密钥这类启动参数仍在环境变量；配置值是明文，不能放机密。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from atlas_appconfig import AppConfig, ConfigKey

from .device.files import FileLimits

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from .config import Settings

__all__ = ["RuntimeConfig", "RuntimeKeys", "WorkspaceLimits"]

_MB = 1024 * 1024


@dataclass(frozen=True)
class RuntimeKeys:
    sandbox_quota: ConfigKey[int]
    sandbox_images: ConfigKey[list[str]]
    bench_images: ConfigKey[list[str]]
    default_agent: ConfigKey[str]
    upload_max_mb: ConfigKey[int]
    quota_mb: ConfigKey[int]
    device_preview_max_mb: ConfigKey[int]
    device_download_max_mb: ConfigKey[int]

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
            # ★ 只是一个指针：默认智能体是普通智能体，权限、共享、编辑与其他智能体完全一样
            default_agent=ConfigKey(
                "platform.default_agent",
                str,
                default="",
                description="首页输入框默认用的智能体 id（空 = 没有默认）",
            ),
            upload_max_mb=ConfigKey(
                "workspace.upload_max_mb",
                int,
                default=settings.workspace_upload_max_mb,
                description="单个文件上传的上限（MB）：工作空间上传、对话里添加文件都按它",
            ),
            quota_mb=ConfigKey(
                "workspace.quota_mb",
                int,
                default=settings.workspace_quota_mb,
                description=(
                    "每人工作空间的存储配额（MB，0 = 不限）：上传时检查，智能体写的文件也算在内"
                ),
            ),
            # 本机文件经设备网关现读，内容多经过 Atlas 一次（本机 CLI 方案 §5.4）
            device_preview_max_mb=ConfigKey(
                "device.preview_max_mb",
                int,
                default=settings.device_preview_max_mb,
                description="网页上预览本机 CLI 会话里文件的大小上限（MB），超过时请到电脑上打开",
            ),
            device_download_max_mb=ConfigKey(
                "device.download_max_mb",
                int,
                default=settings.device_download_max_mb,
                description="网页上下载本机 CLI 会话里文件的大小上限（MB）",
            ),
        )

    def all(self) -> list[ConfigKey[Any]]:
        return [
            self.sandbox_quota,
            self.sandbox_images,
            self.bench_images,
            self.default_agent,
            self.upload_max_mb,
            self.quota_mb,
            self.device_preview_max_mb,
            self.device_download_max_mb,
        ]


@dataclass(frozen=True)
class WorkspaceLimits:
    """上传时检查的两个上限（字节）。quota 为 0 = 不限。"""

    upload_max: int
    quota: int


class RuntimeConfig:
    """Atlas 的运行期配置：一个 AppConfig 加上 Atlas 声明的 key。构造不做 IO。"""

    def __init__(self, settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.keys = RuntimeKeys.of(settings)
        self.config = AppConfig(sessions, self.keys.all())

    async def sandbox_quota(self) -> int:
        return await self.config.get(self.keys.sandbox_quota)

    async def default_agent(self) -> str:
        return await self.config.get(self.keys.default_agent)

    async def set_default_agent(self, agent_id: str, *, by: str) -> None:
        entry = await self.config.entry(self.keys.default_agent.name)
        await self.config.set(
            self.keys.default_agent.name,
            agent_id or None,
            expected_version=entry.item.version,
            by=by,
        )

    async def workspace_limits(self) -> WorkspaceLimits:
        upload, quota = await self.config.get_many(self.keys.upload_max_mb, self.keys.quota_mb)
        return WorkspaceLimits(upload_max=max(upload, 0) * _MB, quota=max(quota, 0) * _MB)

    async def device_file_limits(self) -> FileLimits:
        preview, download = await self.config.get_many(
            self.keys.device_preview_max_mb, self.keys.device_download_max_mb
        )
        return FileLimits(preview_max_mb=max(preview, 0), download_max_mb=max(download, 0))

    async def image_allow_prefixes(self) -> dict[str, list[str]]:
        strict, bench = await self.config.get_many(self.keys.sandbox_images, self.keys.bench_images)
        return {"strict": strict, "bench": bench}
