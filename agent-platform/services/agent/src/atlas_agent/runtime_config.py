"""运行期可改的业务参数（支线 C：应用配置库 atlas_appconfig）。

c4-model/infra-platform/code-appconfig.html。

  系统参数（只有运营中心能改，经运营接口 /ops/v1/configs）
    sandbox.image_allow_prefixes        strict 档可用的镜像前缀
    sandbox.bench_image_allow_prefixes  评测档可用的镜像前缀
    workspace.upload_max_mb             单个文件上传的上限（工作空间、对话里添加文件）
    capacity.default_running_runs       没单独定份额的租户：同时运行的 run（0 = 不限）
    capacity.default_sandboxes          同上：同时存在的沙箱和会话 Pod（0 = 不限）
    capacity.default_storage_mb         同上：工作空间存储（MB，0 = 不限）
  租户设置（每个租户一份，租户管理员改；没改过的用系统值）
    tenant.default_agent                首页输入框默认用的智能体（空 = 没有默认）
    capacity.runs_per_person            每人同时运行的 run（0 = 不限）
    capacity.sandboxes_per_person       每人同时几个沙箱 / 会话 Pod（原生沙箱与会话 Pod 合计）
    capacity.storage_mb_per_person      每人工作空间的存储配额（0 = 不限）
    device.preview_max_mb               网页上预览本机文件的大小上限
    device.download_max_mb              网页上下载本机文件的大小上限
    browser.sandbox_image               开了浏览器的原生智能体用的沙箱镜像
    browser.cli_image                   开了浏览器的 CLI 智能体用的会话 Pod 镜像
    browser.skill                       开了浏览器的智能体自动带上的浏览器技能（slug@版本）
    browser.computer_use_skill          模型能看图时再带上的 computer use 技能（slug@版本）

★ 默认值取部署配置（同名环境变量）：没在运行期改过的，行为和以前一样；管理员经 /v1/configs 改了，
  下一次读就生效（读不缓存），不用重启。
★ 数据库地址、密钥这类启动参数仍在环境变量；配置值是明文，不能放机密。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from atlas_appconfig import AppConfig, ConfigKey, ConfigScope
from atlas_tenancy import TenantScope

from .browser.plan import BrowserConfig, parse_skill_ref
from .device.files import FileLimits

if TYPE_CHECKING:
    from atlas_engine.contracts.spec import SkillRefSpec
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from .config import Settings

__all__ = ["RuntimeConfig", "RuntimeKeys", "WorkspaceLimits"]

logger = logging.getLogger(__name__)

_MB = 1024 * 1024


@dataclass(frozen=True)
class RuntimeKeys:
    sandbox_quota: ConfigKey[int]
    sandbox_images: ConfigKey[list[str]]
    bench_images: ConfigKey[list[str]]
    default_agent: ConfigKey[str]
    upload_max_mb: ConfigKey[int]
    quota_mb: ConfigKey[int]
    runs_per_person: ConfigKey[int]
    default_running_runs: ConfigKey[int]
    default_sandboxes: ConfigKey[int]
    default_storage_mb: ConfigKey[int]
    device_preview_max_mb: ConfigKey[int]
    device_download_max_mb: ConfigKey[int]
    browser_sandbox_image: ConfigKey[str]
    browser_cli_image: ConfigKey[str]
    browser_skill: ConfigKey[str]
    browser_computer_use_skill: ConfigKey[str]

    @classmethod
    def of(cls, settings: Settings) -> RuntimeKeys:
        return cls(
            # ★ 每人上限是租户设置（多租户设计 §9）：租户管理员在本租户份额内定，没定的用系统值。
            #   由原来的系统参数 sandbox.quota_per_user、workspace.quota_mb 换名而来（作用域登记后
            #   不能改），旧值由 migrate-appconfig 搬过来（config_carryover.py）
            sandbox_quota=ConfigKey(
                "capacity.sandboxes_per_person",
                int,
                default=settings.sandbox_quota_per_user,
                description="每人同时几个沙箱 / 会话 Pod（原生沙箱与会话 Pod 合计，0 = 不限）",
                scope=ConfigScope.TENANT,
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
                "tenant.default_agent",
                str,
                default="",
                description="首页输入框默认用的智能体 id（空 = 没有默认）",
                scope=ConfigScope.TENANT,
            ),
            upload_max_mb=ConfigKey(
                "workspace.upload_max_mb",
                int,
                default=settings.workspace_upload_max_mb,
                description="单个文件上传的上限（MB）：工作空间上传、对话里添加文件都按它",
            ),
            quota_mb=ConfigKey(
                "capacity.storage_mb_per_person",
                int,
                default=settings.workspace_quota_mb,
                description=(
                    "每人工作空间的存储配额（MB，0 = 不限）：上传时检查，智能体写的文件也算在内"
                ),
                scope=ConfigScope.TENANT,
            ),
            runs_per_person=ConfigKey(
                "capacity.runs_per_person",
                int,
                default=0,
                description="每人同时运行的 run（0 = 不限）；多出来的排队，不算失败",
                scope=ConfigScope.TENANT,
            ),
            # 租户份额的默认值：运营中心没单独给某个租户定份额时用（0 = 不限）
            default_running_runs=ConfigKey(
                "capacity.default_running_runs",
                int,
                default=0,
                description="没单独定份额的租户：同时运行的 run（0 = 不限）",
            ),
            default_sandboxes=ConfigKey(
                "capacity.default_sandboxes",
                int,
                default=0,
                description="没单独定份额的租户：同时存在的沙箱和会话 Pod（0 = 不限）",
            ),
            default_storage_mb=ConfigKey(
                "capacity.default_storage_mb",
                int,
                default=0,
                description="没单独定份额的租户：工作空间存储（MB，0 = 不限）",
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
            # 浏览器（c4-model/agent/browser/design-browser.html）：改了只影响之后新建的沙箱
            browser_sandbox_image=ConfigKey(
                "browser.sandbox_image",
                str,
                default=settings.browser_sandbox_image,
                description="开了浏览器的原生智能体用的沙箱镜像（空 = 原生智能体不能用浏览器）",
            ),
            browser_cli_image=ConfigKey(
                "browser.cli_image",
                str,
                default=settings.browser_cli_image,
                description=(
                    "开了浏览器的 CLI 智能体用的会话 Pod 镜像（空 = CLI 智能体不能用浏览器）"
                ),
            ),
            browser_skill=ConfigKey(
                "browser.skill",
                str,
                default=settings.browser_skill,
                description="开了浏览器的智能体自动带上的浏览器技能，写成 slug@版本（空 = 不带）",
            ),
            browser_computer_use_skill=ConfigKey(
                "browser.computer_use_skill",
                str,
                default=settings.browser_computer_use_skill,
                description=(
                    "模型能看图时再带上的 computer use 技能，写成 slug@版本（空 = 不带）。"
                    "本机 CLI 智能体用电脑上装的，不看这一项"
                ),
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
            self.runs_per_person,
            self.default_running_runs,
            self.default_sandboxes,
            self.default_storage_mb,
            self.device_preview_max_mb,
            self.device_download_max_mb,
            self.browser_sandbox_image,
            self.browser_cli_image,
            self.browser_skill,
            self.browser_computer_use_skill,
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
        self._settings = settings

    async def sandbox_quota(self) -> int:
        """当前租户的每人沙箱上限。"""
        return await self.config.get(self.keys.sandbox_quota, tenant_uuid=TenantScope.require())

    async def default_agent(self) -> str:
        """当前租户的默认智能体。"""
        return await self.config.get(self.keys.default_agent, tenant_uuid=TenantScope.require())

    async def set_default_agent(self, agent_id: str, *, by: str) -> None:
        tenant = TenantScope.require()
        entry = await self.config.entry(self.keys.default_agent.name, tenant_uuid=tenant)
        await self.config.set(
            self.keys.default_agent.name,
            agent_id or None,
            tenant_uuid=tenant,
            expected_version=entry.item.version,
            by=by,
        )

    async def workspace_limits(self) -> WorkspaceLimits:
        """上传的硬上限（系统参数）和当前租户的每人存储配额（租户设置）。"""
        upload, quota = await self.config.get_many(
            self.keys.upload_max_mb, self.keys.quota_mb, tenant_uuid=TenantScope.require()
        )
        return WorkspaceLimits(upload_max=max(upload, 0) * _MB, quota=max(quota, 0) * _MB)

    async def capacity_defaults(self) -> tuple[int, int, int]:
        """没单独定份额的租户用的份额：(同时运行的 run, 沙箱, 存储字节)，0 = 不限。"""
        runs, sandboxes, storage = await self.config.get_many(
            self.keys.default_running_runs,
            self.keys.default_sandboxes,
            self.keys.default_storage_mb,
        )
        return max(runs, 0), max(sandboxes, 0), max(storage, 0) * _MB

    async def per_person(self) -> tuple[int, int, int]:
        """当前租户的每人上限：(同时运行的 run, 沙箱, 存储字节)，0 = 不限。"""
        runs, sandboxes, storage = await self.config.get_many(
            self.keys.runs_per_person,
            self.keys.sandbox_quota,
            self.keys.quota_mb,
            tenant_uuid=TenantScope.require(),
        )
        return max(runs, 0), max(sandboxes, 0), max(storage, 0) * _MB

    async def device_file_limits(self) -> FileLimits:
        preview, download = await self.config.get_many(
            self.keys.device_preview_max_mb, self.keys.device_download_max_mb
        )
        return FileLimits(preview_max_mb=max(preview, 0), download_max_mb=max(download, 0))

    async def browser_config(self) -> BrowserConfig:
        """浏览器镜像和技能。技能写错了（不是 slug@版本）、当前租户没装这个版本（技能市场
        没拷过来），都当作不带，记日志，不挡住这一轮。"""
        sandbox_image, cli_image, skill, computer_use = await self.config.get_many(
            self.keys.browser_sandbox_image,
            self.keys.browser_cli_image,
            self.keys.browser_skill,
            self.keys.browser_computer_use_skill,
        )
        return BrowserConfig(
            sandbox_image=sandbox_image,
            cli_image=cli_image,
            skill=await self._installed("browser.skill", _skill_ref("browser.skill", skill)),
            computer_use_skill=await self._installed(
                "browser.computer_use_skill",
                _skill_ref("browser.computer_use_skill", computer_use),
            ),
        )

    async def _installed(self, key: str, ref: SkillRefSpec | None) -> SkillRefSpec | None:
        """系统参数指的是技能市场里的版本，租户要装过才有。没装 → 不带（记日志）。

        ★ 技能应用暂时连不上时照旧带上：后面拷技能时会明确报错，不在这里吞掉。
        """
        if ref is None:
            return None
        from .skills import skill_directory

        directory = skill_directory(self._settings)
        if not directory.configured:
            return ref
        try:
            await directory.get(ref.slug, ref.version)
        except LookupError:
            logger.warning(
                "运行期配置 %s 指向的技能 %s@v%s 本租户没有装，当作不带",
                key,
                ref.slug,
                ref.version,
            )
            return None
        except Exception:  # 连不上不在这里判
            logger.warning("查技能 %s@v%s 是否已装失败，照旧带上", ref.slug, ref.version)
        return ref

    async def image_allow_prefixes(self) -> dict[str, list[str]]:
        strict, bench = await self.config.get_many(self.keys.sandbox_images, self.keys.bench_images)
        return {"strict": strict, "bench": bench}


def _skill_ref(key: str, raw: str) -> SkillRefSpec | None:
    try:
        return parse_skill_ref(raw)
    except ValueError:
        logger.warning("运行期配置 %s 格式不对：%r，当作不带这个技能", key, raw)
        return None
