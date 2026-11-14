"""浏览器 · 登录态（c4-model/agent/browser/code-browser-login.html）：这个人在各个网站上的登录态。

    Site            站点 = 可注册域名（按公共后缀表算）：login.taobao.com、.taobao.com
                    都归 taobao.com
    StateBundle     一份登录态，形状同 Playwright 的 storageState：cookies + 各个源的本地存储
    KeyRing         加密密钥，按版本取（部署 Secret）：当前版本加密，旧版本只解密
    StateCipher     AES-256-GCM，附加数据 =（人，站点）：密文挪到别人名下或改了站点就解不开
    SiteLogin       一个人在一个站点上的登录态（聚合根）：密文、捕获时间、过期时间
    LoginRepository browser_login 的读写；写入是带条件的 upsert（只用更新的覆盖）
    LoginVault      入口：save · load · sites · forget

★ 它是浏览器组件最底下的一层：不碰沙箱，也不知道浏览器在哪；什么时候导入导出由宿主和接管决定。
★ 只在内存里有明文：进库前加密，不进日志。
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr
from sqlalchemy import delete, func, select

from ..db.models import BrowserLogin
from ..errors import CapabilityUnavailable
from ..ids import uuid7

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "BrowserNotConfigured",
    "KeyRing",
    "LoginRepository",
    "LoginVault",
    "Sealed",
    "Site",
    "SiteLogin",
    "SiteSummary",
    "StateBundle",
    "StateCipher",
]

logger = logging.getLogger(__name__)

#: 随机数长度（AES-GCM 推荐 12 字节）
_NONCE = 12


class BrowserNotConfigured(CapabilityUnavailable):
    """这个部署没有配置浏览器需要的东西（登录态密钥、画面密钥、浏览器镜像）。

    装配期遇到时整轮失败，消息原样给用户看（CapabilityUnavailable）。
    """


# ---------------------------------------------------------------------------- 站点


@cache
def _psl() -> Any:
    from publicsuffixlist import PublicSuffixList

    return PublicSuffixList()


@dataclass(frozen=True, order=True)
class Site:
    """站点 = 可注册域名。登录态按它分行、按它删除。

    ★ 算不出可注册域名的（IP、localhost、公共后缀本身）就用主机名本身：这类站点也能登录。
    """

    name: str

    @classmethod
    def of(cls, domain: str) -> Site:
        host = domain.strip().lstrip(".").rstrip(".").lower()
        if not host:
            raise ValueError("域名为空")
        try:
            ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            return cls(_psl().privatesuffix(host) or host)
        return cls(host)

    @classmethod
    def of_origin(cls, origin: str) -> Site:
        """https://www.taobao.com:443 → taobao.com"""
        host = urlsplit(origin).hostname
        if not host:
            raise ValueError(f"不是一个源：{origin!r}")
        return cls.of(host)

    def __str__(self) -> str:
        return self.name


# ---------------------------------------------------------------------------- 登录态


@dataclass(frozen=True)
class StateBundle:
    """一份登录态（Playwright storageState 的形状）。cookie、本地存储原样保留，只按站点拆分、合并。

    cookies   [{name, value, domain, path, expires, httpOnly, secure, sameSite}]；
              expires 是秒级时间戳，-1 = 会话 cookie
    origins   [{origin, localStorage: [{name, value}]}]
    """

    cookies: tuple[Mapping[str, Any], ...] = ()
    origins: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def parse(cls, data: Mapping[str, Any] | None) -> StateBundle:
        data = data or {}
        cookies = tuple(c for c in data.get("cookies") or () if isinstance(c, Mapping))
        origins = tuple(o for o in data.get("origins") or () if isinstance(o, Mapping))
        return cls(cookies, origins)

    @classmethod
    def loads(cls, raw: bytes | str) -> StateBundle:
        return cls.parse(json.loads(raw))

    def dump(self) -> dict[str, Any]:
        return {
            "cookies": [dict(c) for c in self.cookies],
            "origins": [dict(o) for o in self.origins],
        }

    def dumps(self) -> bytes:
        return json.dumps(self.dump(), ensure_ascii=False, separators=(",", ":")).encode()

    def empty(self) -> bool:
        return not self.cookies and not self.origins

    def sites(self) -> set[Site]:
        return set(self.split_by_site())

    def split_by_site(self) -> dict[Site, StateBundle]:
        """按站点拆开。cookie 看 domain，本地存储看源的主机名；认不出站点的丢掉。"""
        cookies: dict[Site, list[Mapping[str, Any]]] = {}
        origins: dict[Site, list[Mapping[str, Any]]] = {}
        for cookie in self.cookies:
            try:
                site = Site.of(str(cookie.get("domain") or ""))
            except ValueError:
                continue
            cookies.setdefault(site, []).append(cookie)
        for origin in self.origins:
            try:
                site = Site.of_origin(str(origin.get("origin") or ""))
            except ValueError:
                continue
            origins.setdefault(site, []).append(origin)
        return {
            site: StateBundle(tuple(cookies.get(site, ())), tuple(origins.get(site, ())))
            for site in sorted(cookies.keys() | origins.keys())
        }

    def only(self, sites: Iterable[Site]) -> StateBundle:
        """只留这些站点的。"""
        keep = set(sites)
        return StateBundle.merge(
            part for site, part in self.split_by_site().items() if site in keep
        )

    def without_expired(self, now: datetime) -> StateBundle:
        """去掉已经过期的 cookie（会话 cookie 留着）。"""
        stamp = now.timestamp()
        alive = tuple(c for c in self.cookies if not _expired_cookie(c, stamp))
        return StateBundle(alive, self.origins)

    def expires_at(self) -> datetime | None:
        """cookie 里最晚的过期时间；有会话 cookie 时为 None（它不按时间过期，导入时照样带上）。"""
        stamps = []
        for cookie in self.cookies:
            expires = _expires(cookie)
            if expires is None:
                return None
            stamps.append(expires)
        return datetime.fromtimestamp(max(stamps), UTC) if stamps else None

    @staticmethod
    def merge(parts: Iterable[StateBundle]) -> StateBundle:
        cookies: list[Mapping[str, Any]] = []
        origins: list[Mapping[str, Any]] = []
        for part in parts:
            cookies.extend(part.cookies)
            origins.extend(part.origins)
        return StateBundle(tuple(cookies), tuple(origins))


def _expires(cookie: Mapping[str, Any]) -> float | None:
    """cookie 的过期时间戳；会话 cookie（-1、0、没有）返回 None。"""
    try:
        value = float(cookie.get("expires", -1))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _expired_cookie(cookie: Mapping[str, Any], now: float) -> bool:
    expires = _expires(cookie)
    return expires is not None and expires <= now


# ---------------------------------------------------------------------------- 加密


class KeyRing:
    """登录态的加密密钥，按版本取。

    来自部署 Secret（Settings.browser_state_keys），不进库、不进日志。
    """

    def __init__(self, keys: Mapping[int, SecretStr]) -> None:
        self._keys: dict[int, bytes] = {}
        for version, secret in keys.items():
            raw = base64.b64decode(secret.get_secret_value())
            if len(raw) != 32:
                raise ValueError(f"登录态密钥版本 {version} 不是 32 字节（AES-256）")
            self._keys[int(version)] = raw

    def configured(self) -> bool:
        return bool(self._keys)

    def current(self) -> tuple[int, bytes]:
        if not self._keys:
            raise BrowserNotConfigured("没有配置登录态的加密密钥（BROWSER_STATE_KEYS）")
        version = max(self._keys)
        return version, self._keys[version]

    def get(self, version: int) -> bytes:
        try:
            return self._keys[version]
        except KeyError as exc:
            raise BrowserNotConfigured(f"登录态密钥版本 {version} 不在当前配置里") from exc


@dataclass(frozen=True)
class Sealed:
    """一份密文：随机数 + 密文（含认证标签），和加密用的密钥版本。"""

    data: bytes
    key_version: int


class StateCipher:
    """AES-256-GCM。附加数据是（人，站点）：一行被挪到别人名下或改了站点，解密失败。"""

    def __init__(self, keys: KeyRing) -> None:
        self._keys = keys

    def seal(self, user: UUID, site: Site, data: bytes) -> Sealed:
        version, key = self._keys.current()
        nonce = os.urandom(_NONCE)
        return Sealed(nonce + AESGCM(key).encrypt(nonce, data, _aad(user, site)), version)

    def open(self, user: UUID, site: Site, sealed: Sealed) -> bytes:
        key = self._keys.get(sealed.key_version)
        nonce, body = sealed.data[:_NONCE], sealed.data[_NONCE:]
        return AESGCM(key).decrypt(nonce, body, _aad(user, site))


def _aad(user: UUID, site: Site) -> bytes:
    return f"{user}|{site.name}".encode()


# ---------------------------------------------------------------------------- 聚合与仓储


@dataclass(frozen=True)
class SiteLogin:
    """一个人在一个站点上的登录态。只按「捕获时间更新」覆盖。"""

    user_uuid: UUID
    site: Site
    sealed: Sealed
    captured_at: datetime
    captured_from: UUID | None = None
    expires_at: datetime | None = None

    def newer_than(self, other: SiteLogin) -> bool:
        return self.captured_at > other.captured_at

    def expired(self, now: datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= now

    @classmethod
    def of_row(cls, row: BrowserLogin) -> SiteLogin:
        return cls(
            user_uuid=row.user_uuid,
            site=Site(row.site),
            sealed=Sealed(bytes(row.sealed), row.key_version),
            captured_at=row.captured_at,
            captured_from=row.captured_from,
            expires_at=row.expires_at,
        )


@dataclass(frozen=True)
class SiteSummary:
    """「我的登录」里的一行：没有内容，只有站点和时间。"""

    site: str
    captured_at: datetime
    captured_from: UUID | None
    expires_at: datetime | None
    expired: bool


class LoginRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_if_newer(self, login: SiteLogin) -> bool:
        """库里没有、或库里的捕获时间更早才写。返回写没写。

        ★ 条件写在一条语句里：并发导出同一个站点时不用加锁，旧的不会盖掉新的。
        """
        values = {
            "user_uuid": login.user_uuid,
            "site": login.site.name,
            "sealed": login.sealed.data,
            "key_version": login.sealed.key_version,
            "captured_at": login.captured_at,
            "captured_from": login.captured_from,
            "expires_at": login.expires_at,
        }
        table = BrowserLogin.__table__
        dialect = self._session.bind.dialect.name if self._session.bind else "postgresql"
        newer = ("sealed", "key_version", "captured_at", "captured_from", "expires_at")
        if dialect in ("mysql", "mariadb"):
            from sqlalchemy.dialects.mysql import insert as mysql_insert

            stmt: Any = mysql_insert(table).values(login_uuid=uuid7(), **values)
            fresher = table.c.captured_at < stmt.inserted.captured_at
            stmt = stmt.on_duplicate_key_update(
                {c: func.if_(fresher, stmt.inserted[c], table.c[c]) for c in newer}
                | {"updated_at": func.if_(fresher, func.now(), table.c.updated_at)}
            )
            # MySQL 的 rowcount：插入 1、改了 2、没改 0
            return bool((await self._session.execute(stmt)).rowcount)
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(table).values(login_uuid=uuid7(), **values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_uuid", "site"],
            set_={c: stmt.excluded[c] for c in newer} | {"updated_at": func.now()},
            where=table.c.captured_at < stmt.excluded.captured_at,
        )
        return bool((await self._session.execute(stmt)).rowcount)

    async def of_user(self, user: UUID) -> list[SiteLogin]:
        stmt = (
            select(BrowserLogin).where(BrowserLogin.user_uuid == user).order_by(BrowserLogin.site)
        )
        return [SiteLogin.of_row(r) for r in (await self._session.execute(stmt)).scalars()]

    async def sites_of(self, user: UUID) -> set[Site]:
        stmt = select(BrowserLogin.site).where(BrowserLogin.user_uuid == user)
        return {Site(s) for s in (await self._session.execute(stmt)).scalars()}

    async def get(self, user: UUID, site: Site) -> SiteLogin | None:
        stmt = select(BrowserLogin).where(
            BrowserLogin.user_uuid == user, BrowserLogin.site == site.name
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return None if row is None else SiteLogin.of_row(row)

    async def delete(self, user: UUID, site: Site) -> bool:
        stmt = delete(BrowserLogin).where(
            BrowserLogin.user_uuid == user, BrowserLogin.site == site.name
        )
        return bool((await self._session.execute(stmt)).rowcount)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------- 入口


class LoginVault:
    """登录态的入口。构造不做 IO；每次调用自己开会话、自己提交。"""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        cipher: StateCipher,
        *,
        clock: Any = None,
    ) -> None:
        self._sessions = sessions
        self._cipher = cipher
        self._clock = clock or (lambda: datetime.now(UTC))

    async def save(
        self,
        user: UUID,
        bundle: StateBundle,
        *,
        sites: Iterable[Site] | None = None,
        thread: UUID | None = None,
        captured_at: datetime | None = None,
    ) -> list[Site]:
        """按站点拆开、加密，只写比库里新的。返回写进去的站点。

        sites 给了（接管交还）：新增或刷新这些站点。
        没给（段末刷新、退役前）：只刷新已经存过的站点，库里没有的一律丢掉 —— 模型自己逛过的网站、
        广告和统计的 cookie 不进库。
        """
        at = captured_at or self._clock()
        parts = bundle.split_by_site()
        async with self._sessions() as session:
            repo = LoginRepository(session)
            wanted = set(sites) if sites is not None else await repo.sites_of(user)
            written: list[Site] = []
            for site, part in parts.items():
                if site not in wanted:
                    continue
                login = SiteLogin(
                    user_uuid=user,
                    site=site,
                    sealed=self._cipher.seal(user, site, part.dumps()),
                    captured_at=at,
                    captured_from=thread,
                    expires_at=part.expires_at(),
                )
                if await repo.upsert_if_newer(login):
                    written.append(site)
            await session.commit()
        return written

    async def load(self, user: UUID) -> StateBundle:
        """解密这个人的全部站点，跳过已过期的（和过期的 cookie），合并成一份。

        解不开的跳过并记日志。
        """
        now = self._clock()
        async with self._sessions() as session:
            logins = await LoginRepository(session).of_user(user)
        parts = []
        for login in logins:
            if login.expired(now):
                continue
            try:
                plain = self._cipher.open(user, login.site, login.sealed)
            except (InvalidTag, BrowserNotConfigured, ValueError):
                logger.warning("用户 %s 在 %s 上的登录态解不开，跳过", user, login.site)
                continue
            parts.append(StateBundle.loads(plain).without_expired(now))
        return StateBundle.merge(parts)

    async def stored_sites(self, user: UUID) -> set[Site]:
        """存过哪些站点（段末刷新、退役前只导出这些）。"""
        async with self._sessions() as session:
            return await LoginRepository(session).sites_of(user)

    async def stored_origins(self, user: UUID) -> set[str]:
        """存过本地存储的源（导出时告诉守护：没开着页面的源它不知道）。"""
        bundle = await self.load(user)
        return {str(o["origin"]) for o in bundle.origins if o.get("origin")}

    async def sites(self, user: UUID) -> list[SiteSummary]:
        now = self._clock()
        async with self._sessions() as session:
            logins = await LoginRepository(session).of_user(user)
        return [
            SiteSummary(
                site=login.site.name,
                captured_at=login.captured_at,
                captured_from=login.captured_from,
                expires_at=login.expires_at,
                expired=login.expired(now),
            )
            for login in logins
        ]

    async def forget(self, user: UUID, site: Site) -> bool:
        """只删库里的；正在跑的浏览器由宿主去清。"""
        async with self._sessions() as session:
            removed = await LoginRepository(session).delete(user, site)
            await session.commit()
        return removed
