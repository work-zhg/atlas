"""浏览器 · 登录态（c4-model/agent/browser/code-browser-login.html）。

站点      可注册域名：子域名、带点的 cookie 域都归一处；IP、localhost 用主机名本身
拆分合并  cookie 看 domain、本地存储看源；只留某些站点；去掉过期 cookie；最晚过期时间
加密      AES-256-GCM，附加数据绑人和站点：换人、换站点解不开；旧版本密钥照样能解
入口      交还时按给的站点存；刷新只更新已存过的站点；只用更新的覆盖；导入跳过过期的；
          我的登录列出、删除
"""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from atlas_agent.browser.login import (
    BrowserNotConfigured,
    KeyRing,
    LoginVault,
    Site,
    StateBundle,
    StateCipher,
)
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.ids import uuid7
from cryptography.exceptions import InvalidTag
from pydantic import SecretStr

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=30)


def _key() -> SecretStr:
    return SecretStr(base64.b64encode(os.urandom(32)).decode())


def _cookie(domain: str, name: str, value: str = "v", expires: float = -1) -> dict:
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
        "expires": expires,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }


def _bundle() -> StateBundle:
    return StateBundle.parse(
        {
            "cookies": [
                _cookie(".taobao.com", "cookie2", expires=LATER.timestamp()),
                _cookie("login.taobao.com", "_tb_token_"),
                _cookie("zwfw.beijing.gov.cn", "SESSION", expires=LATER.timestamp()),
                _cookie(".doubleclick.net", "IDE", expires=LATER.timestamp()),
            ],
            "origins": [
                {
                    "origin": "https://www.taobao.com",
                    "localStorage": [{"name": "token", "value": "t"}],
                },
            ],
        }
    )


# ---------------------------------------------------------------------------- 站点


@pytest.mark.parametrize(
    ("domain", "site"),
    [
        ("login.taobao.com", "taobao.com"),
        (".taobao.com", "taobao.com"),
        ("WWW.TaoBao.com.", "taobao.com"),
        ("zwfw.beijing.gov.cn", "beijing.gov.cn"),
        ("a.b.co.uk", "b.co.uk"),
        ("foo.github.io", "foo.github.io"),
        ("localhost", "localhost"),
        ("192.168.0.1", "192.168.0.1"),
    ],
)
def test_site_is_the_registrable_domain(domain: str, site: str) -> None:
    assert Site.of(domain) == Site(site)


def test_site_of_origin() -> None:
    assert Site.of_origin("https://www.taobao.com:443") == Site("taobao.com")
    with pytest.raises(ValueError):
        Site.of_origin("not a url")


# ---------------------------------------------------------------------------- 拆分合并


def test_split_by_site_groups_cookies_and_local_storage() -> None:
    parts = _bundle().split_by_site()
    assert set(parts) == {Site("taobao.com"), Site("beijing.gov.cn"), Site("doubleclick.net")}
    taobao = parts[Site("taobao.com")]
    assert {c["name"] for c in taobao.cookies} == {"cookie2", "_tb_token_"}
    assert [o["origin"] for o in taobao.origins] == ["https://www.taobao.com"]
    assert not parts[Site("doubleclick.net")].origins


def test_only_keeps_the_given_sites_and_merge_roundtrips() -> None:
    bundle = _bundle()
    only = bundle.only([Site("taobao.com")])
    assert only.sites() == {Site("taobao.com")}
    merged = StateBundle.merge(bundle.split_by_site().values())
    assert len(merged.cookies) == len(bundle.cookies)
    assert StateBundle.loads(merged.dumps()) == merged


def test_expiry() -> None:
    expired = _cookie("a.com", "old", expires=(NOW - timedelta(days=1)).timestamp())
    fresh = _cookie("a.com", "new", expires=LATER.timestamp())
    session = _cookie("a.com", "sess")
    bundle = StateBundle.parse({"cookies": [expired, fresh, session]})
    assert {c["name"] for c in bundle.without_expired(NOW).cookies} == {"new", "sess"}
    # 有会话 cookie：不按时间过期
    assert bundle.expires_at() is None
    assert StateBundle.parse({"cookies": [expired, fresh]}).expires_at() == LATER


# ---------------------------------------------------------------------------- 加密


def test_cipher_binds_user_and_site() -> None:
    cipher = StateCipher(KeyRing({1: _key()}))
    alice, bob = uuid7(), uuid7()
    sealed = cipher.seal(alice, Site("taobao.com"), b"secret")
    assert b"secret" not in sealed.data
    assert cipher.open(alice, Site("taobao.com"), sealed) == b"secret"
    with pytest.raises(InvalidTag):
        cipher.open(bob, Site("taobao.com"), sealed)
    with pytest.raises(InvalidTag):
        cipher.open(alice, Site("jd.com"), sealed)


def test_cipher_rotates_keys() -> None:
    old = KeyRing({1: _key()})
    sealed = StateCipher(old).seal(uuid7(), Site("a.com"), b"x")
    assert sealed.key_version == 1
    rotated = KeyRing({1: SecretStr(base64.b64encode(old.get(1)).decode()), 2: _key()})
    assert StateCipher(rotated).seal(uuid7(), Site("a.com"), b"x").key_version == 2


def test_keyring_requires_configuration_and_32_bytes() -> None:
    with pytest.raises(BrowserNotConfigured):
        KeyRing({}).current()
    with pytest.raises(ValueError):
        KeyRing({1: SecretStr(base64.b64encode(b"short").decode())})


# ---------------------------------------------------------------------------- 入口（库）


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
async def vault(clean_db: None) -> AsyncIterator[tuple[LoginVault, _Clock]]:
    clock = _Clock()
    yield LoginVault(get_sessionmaker(), StateCipher(KeyRing({1: _key()})), clock=clock), clock


async def test_hand_back_saves_only_the_given_sites(vault: tuple[LoginVault, _Clock]) -> None:
    v, _ = vault
    user, thread = uuid7(), uuid7()
    written = await v.save(user, _bundle(), sites=[Site("taobao.com")], thread=thread)
    assert written == [Site("taobao.com")]
    [summary] = await v.sites(user)
    assert summary.site == "taobao.com"
    assert summary.captured_from == thread
    assert summary.expires_at is None  # _tb_token_ 是会话 cookie
    loaded = await v.load(user)
    assert loaded.sites() == {Site("taobao.com")}
    assert {c["name"] for c in loaded.cookies} == {"cookie2", "_tb_token_"}


async def test_refresh_only_updates_sites_already_saved(vault: tuple[LoginVault, _Clock]) -> None:
    v, clock = vault
    user = uuid7()
    await v.save(user, _bundle(), sites=[Site("taobao.com")])
    clock.now = NOW + timedelta(minutes=5)
    renewed = StateBundle.parse(
        {"cookies": [_cookie(".taobao.com", "cookie2", value="renewed"), _cookie("jd.com", "x")]}
    )
    assert await v.save(user, renewed) == [Site("taobao.com")]
    loaded = await v.load(user)
    assert loaded.sites() == {Site("taobao.com")}
    assert [c["value"] for c in loaded.cookies] == ["renewed"]


async def test_older_capture_never_overwrites_newer(vault: tuple[LoginVault, _Clock]) -> None:
    v, _ = vault
    user = uuid7()
    site = [Site("taobao.com")]
    newer = StateBundle.parse({"cookies": [_cookie("taobao.com", "c", value="new")]})
    older = StateBundle.parse({"cookies": [_cookie("taobao.com", "c", value="old")]})
    assert await v.save(user, newer, sites=site, captured_at=NOW) == site
    assert await v.save(user, older, sites=site, captured_at=NOW - timedelta(seconds=1)) == []
    assert [c["value"] for c in (await v.load(user)).cookies] == ["new"]


async def test_load_skips_expired_sites_and_forget_deletes(
    vault: tuple[LoginVault, _Clock],
) -> None:
    v, clock = vault
    user = uuid7()
    await v.save(user, _bundle(), sites=[Site("beijing.gov.cn"), Site("taobao.com")])
    clock.now = LATER + timedelta(days=1)
    summaries = {s.site: s for s in await v.sites(user)}
    assert summaries["beijing.gov.cn"].expired
    assert not summaries["taobao.com"].expired
    loaded = await v.load(user)
    assert loaded.sites() == {Site("taobao.com")}
    # 过期的 cookie2 去掉，会话 cookie 留着
    assert {c["name"] for c in loaded.cookies} == {"_tb_token_"}
    assert await v.forget(user, Site("taobao.com"))
    assert not await v.forget(user, Site("taobao.com"))
    assert [s.site for s in await v.sites(user)] == ["beijing.gov.cn"]


async def test_other_users_logins_are_invisible(vault: tuple[LoginVault, _Clock]) -> None:
    v, _ = vault
    alice, bob = uuid7(), uuid7()
    await v.save(alice, _bundle(), sites=[Site("taobao.com")])
    assert (await v.load(bob)).empty()
    assert await v.sites(bob) == []
