"""技能目录（设计 §7.1）。

两层缓存，依据是配置服务的**不可变契约**：

  版本内容（描述、文件清单、content_hash）  内存 + 磁盘，永久
      已发布版本永不修改。将来有人想给它加 TTL，要先推翻那条契约。
  可变状态（status、下架清单、latest）      TTL（默认 60s）；拉取失败继续用上一次结果
      下架要在一分钟内生效；配置服务短暂不可用不能让运行时停摆。

配置服务不可用时：用过的版本照常装配；只有首次用到的版本会失败。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from atlas_config.schemas import SkillCatalogItem, SkillVersionOut

from .client import ConfigClient, ConfigNotFound, ConfigPlaneUnavailable

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger(__name__)

__all__ = [
    "FakeSkillDirectory",
    "HttpSkillDirectory",
    "SkillDirectory",
    "SkillNotFound",
    "UnconfiguredSkillDirectory",
    "override_skill_directory",
    "skill_directory",
]


class SkillNotFound(LookupError):
    """配置服务明确说没有这个技能 / 版本（或它只是个未发布的上传）。"""


class SkillDirectory(Protocol):
    #: False = 没接配置服务（兼容接入前的行为，见 Settings.config_base_url）
    configured: bool

    async def catalog(self) -> list[SkillCatalogItem]: ...
    async def get(self, slug: str, version: int) -> SkillVersionOut: ...
    async def latest(self, slug: str) -> SkillVersionOut | None: ...
    async def revoked(self) -> frozenset[tuple[str, int]]: ...


# ───────────────────────────────────────────── 实现


class HttpSkillDirectory:
    configured = True

    def __init__(
        self, client: ConfigClient, *, cache_dir: Path | None, status_ttl_s: float
    ) -> None:
        self._client = client
        self._cache_dir = cache_dir
        self._ttl = status_ttl_s
        self._content: dict[tuple[str, int], SkillVersionOut] = {}
        self._catalog: list[SkillCatalogItem] | None = None
        self._catalog_at = 0.0

    async def catalog(self) -> list[SkillCatalogItem]:
        if self._catalog is not None and time.monotonic() - self._catalog_at < self._ttl:
            return self._catalog
        try:
            raw = await self._client.get("/internal/skills/catalog")
        except ConfigPlaneUnavailable:
            if self._catalog is None:
                raise
            # ★ 可变状态拉不到时用上一次的：下架晚生效一会儿，好过整个运行时停摆
            logger.warning("技能目录刷新失败，继续使用 %.0fs 前的结果", self._age())
            self._catalog_at = time.monotonic()  # 一个 TTL 后再试，不要每次调用都去撞
            return self._catalog
        self._catalog = [SkillCatalogItem.model_validate(item) for item in raw]
        self._catalog_at = time.monotonic()
        return self._catalog

    async def get(self, slug: str, version: int) -> SkillVersionOut:
        content = self._content.get((slug, version)) or self._from_disk(slug, version)
        if content is None:
            try:
                raw = await self._client.get(f"/internal/skills/{slug}/versions/{version}")
            except ConfigNotFound as exc:
                raise SkillNotFound(f"技能 {slug} v{version} 不存在或未发布") from exc
            content = SkillVersionOut.model_validate(raw)
            self._to_disk(content)
        self._content[(slug, version)] = content
        status = await self._status(slug, version)
        return content if status is None else content.model_copy(update={"status": status})

    async def latest(self, slug: str) -> SkillVersionOut | None:
        item = next((c for c in await self.catalog() if c.slug == slug), None)
        if item is None or item.latest is None:
            return None
        return await self.get(slug, item.latest)

    async def revoked(self) -> frozenset[tuple[str, int]]:
        return frozenset(
            (item.slug, v.version)
            for item in await self.catalog()
            for v in item.versions
            if v.status == "revoked"
        )

    # ------------------------------------------------------------------ 内部

    async def _status(self, slug: str, version: int) -> str | None:
        try:
            catalog = await self.catalog()
        except ConfigPlaneUnavailable:
            return None  # 连目录都没拉到过：用内容里记录的状态
        for item in catalog:
            if item.slug == slug:
                for v in item.versions:
                    if v.version == version:
                        return v.status
        return None

    def _age(self) -> float:
        return time.monotonic() - self._catalog_at

    def _path(self, slug: str, version: int) -> Path | None:
        return (
            None
            if self._cache_dir is None
            else self._cache_dir / "skills" / slug / f"{version}.json"
        )

    def _from_disk(self, slug: str, version: int) -> SkillVersionOut | None:
        path = self._path(slug, version)
        if path is None or not path.exists():
            return None
        try:
            return SkillVersionOut.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("技能缓存损坏，丢弃：%s", path, exc_info=True)
            return None

    def _to_disk(self, content: SkillVersionOut) -> None:
        path = self._path(content.slug, content.version)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(content.model_dump_json(), encoding="utf-8")
            tmp.replace(path)  # 原子替换：并发写入不会留下半个文件
        except OSError:
            logger.warning("写技能缓存失败（不影响本次使用）：%s", path, exc_info=True)


class UnconfiguredSkillDirectory:
    """没接配置服务。技能照旧按 spec 里钉死的 (slug, version) 投送，只是查不到元数据。"""

    configured = False

    async def catalog(self) -> list[SkillCatalogItem]:
        return []

    async def get(self, slug: str, version: int) -> SkillVersionOut:
        raise ConfigPlaneUnavailable("未配置配置服务（CONFIG_BASE_URL），无法查询技能元数据")

    async def latest(self, slug: str) -> SkillVersionOut | None:
        return None

    async def revoked(self) -> frozenset[tuple[str, int]]:
        return frozenset()


class FakeSkillDirectory:
    """内存实现，单测用：不起 atlas-config 也能测运行时的技能逻辑。"""

    configured = True

    def __init__(self, versions: Iterable[SkillVersionOut] = ()) -> None:
        self.versions: dict[tuple[str, int], SkillVersionOut] = {
            (v.slug, v.version): v for v in versions
        }

    def add(self, slug: str, version: int, description: str = "", **kw: object) -> SkillVersionOut:
        v = SkillVersionOut(
            slug=slug,
            version=version,
            status=kw.pop("status", "published"),  # type: ignore[arg-type]
            description=description or f"{slug} 的描述",
            content_hash=f"sha256:{slug}{version}",
            size_bytes=1,
            file_count=1,
            has_scripts=False,
            **kw,  # type: ignore[arg-type]
        )
        self.versions[(slug, version)] = v
        return v

    def set_status(self, slug: str, version: int, status: str) -> None:
        self.versions[(slug, version)] = self.versions[(slug, version)].model_copy(
            update={"status": status}
        )

    async def catalog(self) -> list[SkillCatalogItem]:
        from atlas_config.schemas import SkillVersionBrief

        slugs = sorted({s for s, _ in self.versions})
        out = []
        for slug in slugs:
            vs = sorted(
                (v for (s, _), v in self.versions.items() if s == slug), key=lambda v: v.version
            )
            latest = next((v for v in reversed(vs) if v.status == "published"), None)
            out.append(
                SkillCatalogItem(
                    slug=slug,
                    source="builtin",
                    latest=latest.version if latest else None,
                    description=latest.description if latest else None,
                    has_scripts=latest.has_scripts if latest else None,
                    versions=[SkillVersionBrief(version=v.version, status=v.status) for v in vs],
                )
            )
        return out

    async def get(self, slug: str, version: int) -> SkillVersionOut:
        found = self.versions.get((slug, version))
        if found is None:
            raise SkillNotFound(f"技能 {slug} v{version} 不存在或未发布")
        return found

    async def latest(self, slug: str) -> SkillVersionOut | None:
        item = next((c for c in await self.catalog() if c.slug == slug), None)
        return None if item is None or item.latest is None else self.versions[(slug, item.latest)]

    async def revoked(self) -> frozenset[tuple[str, int]]:
        return frozenset(k for k, v in self.versions.items() if v.status == "revoked")


# ───────────────────────────────────────────── 进程级单例

_override: SkillDirectory | None = None
_instances: dict[tuple[str, str, str, float], HttpSkillDirectory] = {}


def override_skill_directory(directory: SkillDirectory | None) -> None:
    """测试用：让所有调用方拿到同一个假目录。传 None 恢复。"""
    global _override
    _override = directory


def skill_directory(settings: Settings) -> SkillDirectory:
    """★ 进程级单例：缓存要跨请求、跨 run 共享，每次新建就等于没有缓存。"""
    if _override is not None:
        return _override
    if not settings.config_base_url:
        return UnconfiguredSkillDirectory()
    token = (
        settings.config_internal_token.get_secret_value() if settings.config_internal_token else ""
    )
    key = (
        settings.config_base_url,
        token,
        str(settings.configplane_cache_dir),
        settings.configplane_status_ttl_s,
    )
    if key not in _instances:
        _instances[key] = HttpSkillDirectory(
            ConfigClient(settings.config_base_url, token or None),
            cache_dir=settings.configplane_cache_dir,
            status_ttl_s=settings.configplane_status_ttl_s,
        )
    return _instances[key]
