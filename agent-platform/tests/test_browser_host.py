"""浏览器 · 宿主（c4-model/agent/browser/code-browser-host.html）。

附加件扩展点   通用供给建好后回调 on_created（只对它认的请求）、失败按建失败处理；退役前回调
              before_retire，失败不拦退役
共享键         带浏览器的原生沙箱另成一组
守护客户端     起守护 → 等就绪 → 导入（文件传完即删）；导出、关接管窗口取走结果文件；失败带原因
画面口令       按 sandbox_uuid 派生、可复现
附加件         建好后导入登录态并登记宿主；checkpoint 只导出存过的站点；退役前导出再注销
换算与校验     开了才换镜像、加技能；保存时要 Shell、不能配自定义镜像、本机 CLI 不行
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from atlas_agent.browser.daemon import Captured, DaemonClient, DaemonError, ViewKey
from atlas_agent.browser.host import BrowserAddon, HostRegistry
from atlas_agent.browser.login import KeyRing, LoginVault, Site, StateBundle, StateCipher
from atlas_agent.browser.plan import BrowserConfig, BrowserPlan, check_browser, parse_skill_ref
from atlas_agent.config import get_settings
from atlas_agent.db.models import SandboxAccountRow, SandboxBindingRow, UserSandboxRow
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.ids import uuid7
from atlas_agent.redisx import make_redis
from atlas_agent.sandbox.base.execution.values import CommandStatus, LogChunk
from atlas_agent.sandbox.base.lifecycle.spec import CreateSpec
from atlas_agent.sandbox.native import NativeSandboxKind
from atlas_agent.sandbox.provisioning.model import (
    BizRef,
    BizType,
    QuotaPolicy,
    SandboxRef,
    Sharing,
)
from atlas_agent.sandbox.provisioning.service import SandboxProvisioning
from atlas_engine.contracts import InvalidSpec
from atlas_engine.contracts.spec import (
    AgentSpec,
    CliSpec,
    ModelSpec,
    SandboxSpec,
    SkillRefSpec,
)
from pydantic import SecretStr
from sqlalchemy import delete, select

from tests.opensandbox_fake import FakeOpenSandbox
from tests.sandbox_env import published_template, runtime_for, simple_spec

# ---------------------------------------------------------------------------- 附加件扩展点


@dataclass(frozen=True)
class _Req:
    browser: bool


class _Kind:
    biz_type = BizType.NATIVE

    def __init__(self, template: str) -> None:
        self.template = template

    async def admit(self, req: Any) -> None:
        return None

    def build(self, req: Any, biz: BizRef) -> CreateSpec:
        return simple_spec(self.template, user=str(biz.user_uuid))

    def share(self, req: Any) -> Sharing | None:
        return None

    async def attach(self, ref: SandboxRef, req: Any, *, created: bool) -> None:
        return None

    async def ready(self, ref: SandboxRef) -> None:
        return None


class _Addon:
    def __init__(self) -> None:
        self.created: list[tuple[UUID, BizType, UUID]] = []
        self.retired: list[tuple[UUID, str]] = []
        self.fail_created = False
        self.fail_retire = False

    def applies(self, req: Any) -> bool:
        return getattr(req, "browser", False) is True

    async def on_created(self, ref: SandboxRef, req: Any, *, biz_type: BizType, user: UUID) -> None:
        if self.fail_created:
            raise RuntimeError("守护起不来")
        self.created.append((ref.sandbox_uuid, biz_type, user))

    async def before_retire(self, ref: SandboxRef, reason: str) -> None:
        self.retired.append((ref.sandbox_uuid, reason))
        if self.fail_retire:
            raise RuntimeError("导出失败")


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[FakeOpenSandbox]:
    f = FakeOpenSandbox(tmp_path)
    yield f
    f.close()


@pytest.fixture
async def prov(fake: FakeOpenSandbox) -> AsyncIterator[tuple[SandboxProvisioning, _Addon]]:
    redis = make_redis(get_settings())
    users: list[UUID] = []
    async with published_template() as template:
        p = SandboxProvisioning(
            runtime_for(fake, redis, template), get_sessionmaker(), quota=QuotaPolicy(5), cache_s=0
        )
        p.register(_Kind(template.code))
        addon = _Addon()
        p.register_addon(addon)
        p.users = users  # type: ignore[attr-defined]
        yield p, addon
        async with get_sessionmaker()() as session:
            for user in users:
                ids = list(
                    await session.scalars(
                        select(UserSandboxRow.sandbox_uuid).where(UserSandboxRow.user_uuid == user)
                    )
                )
                await session.execute(
                    delete(SandboxBindingRow).where(SandboxBindingRow.sandbox_uuid.in_(ids))
                )
                await session.execute(
                    delete(UserSandboxRow).where(UserSandboxRow.user_uuid == user)
                )
                await session.execute(
                    delete(SandboxAccountRow).where(SandboxAccountRow.user_uuid == user)
                )
            await session.commit()
    await redis.aclose()


def _biz(p: SandboxProvisioning) -> BizRef:
    user = uuid7()
    p.users.append(user)  # type: ignore[attr-defined]
    return BizRef(BizType.NATIVE, uuid7(), user)


async def test_addon_runs_once_after_creation_for_requests_it_applies_to(
    prov: tuple[SandboxProvisioning, _Addon],
) -> None:
    p, addon = prov
    biz = _biz(p)
    ref = await p.ensure(biz, _Req(browser=True))
    assert addon.created == [(ref.sandbox_uuid, BizType.NATIVE, biz.user_uuid)]
    # 复用不再回调
    assert await p.ensure(biz, _Req(browser=True)) == ref
    assert len(addon.created) == 1
    # 不是带浏览器的请求：不管
    await p.ensure(_biz(p), _Req(browser=False))
    assert len(addon.created) == 1


async def test_addon_failure_is_a_failed_creation(
    prov: tuple[SandboxProvisioning, _Addon], fake: FakeOpenSandbox
) -> None:
    p, addon = prov
    addon.fail_created = True
    biz = _biz(p)
    with pytest.raises(RuntimeError, match="守护起不来"):
        await p.ensure(biz, _Req(browser=True))
    assert await p.current(biz) is None
    assert fake.deleted  # 退役了，下次 ensure 换一个重建
    # 退役前的回调照样有（附加件自己判断有没有要导出的）
    assert [reason for _, reason in addon.retired] == ["broken"]


async def test_before_retire_runs_on_release_and_failure_does_not_block(
    prov: tuple[SandboxProvisioning, _Addon], fake: FakeOpenSandbox
) -> None:
    p, addon = prov
    addon.fail_retire = True
    biz = _biz(p)
    ref = await p.ensure(biz, _Req(browser=True))
    await p.release(biz, reason="idle")
    assert addon.retired == [(ref.sandbox_uuid, "idle")]
    assert await p.describe(ref) is None


def test_browser_sandboxes_share_among_themselves_only() -> None:
    kind = NativeSandboxKind(get_settings(), None, None)  # type: ignore[arg-type]
    plain = kind.share(SandboxSpec(image="img"))
    browser = kind.share(SandboxSpec(image="img", browser=True))
    assert plain is not None and browser is not None
    assert plain.key != browser.key
    assert kind.share(SandboxSpec(profile="bench")) is None


# ---------------------------------------------------------------------------- 守护客户端


class _Files:
    def __init__(self, store: dict[str, bytes]) -> None:
        self.store = store

    def mkdir(self, path: str) -> None:
        return None

    def upload(self, path: str, data: bytes) -> None:
        self.store[path] = data

    def download(self, path: str) -> bytes | None:
        return self.store.get(path)

    def remove(self, paths: list[str]) -> None:
        for path in paths:
            self.store.pop(path, None)

    def close(self) -> None:
        return None


class _FakeExec:
    """按命令脚本回结果：script(argv) → (退出码, stdout, 要写的文件)。"""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.spawned: list[tuple[str, dict[str, str]]] = []
        self.results: dict[str, tuple[int, bytes]] = {}
        self.script: Any = None

    async def spawn(self, sandbox_uuid: UUID, cmd: Any) -> str:
        cmd_id = f"c{len(self.spawned)}"
        self.spawned.append((cmd.command, dict(cmd.env)))
        if " serve " in f" {cmd.command} ":
            return cmd_id
        argv = cmd.command.split()[1:]
        code, out, writes = self.script(argv, self.files) if self.script else (0, b"{}", {})
        self.files.update(writes)
        self.results[cmd_id] = (code, out)
        return cmd_id

    async def status(self, sandbox_uuid: UUID, cmd_id: str) -> CommandStatus:
        code, _ = self.results[cmd_id]
        return CommandStatus(running=False, exit_code=code, error=None)

    async def read(self, sandbox_uuid: UUID, cmd_id: str, cursor: int) -> LogChunk:
        _, out = self.results[cmd_id]
        return LogChunk(out[cursor:], len(out))

    async def interrupt(self, sandbox_uuid: UUID, cmd_id: str) -> None:
        return None

    async def open_files(self, sandbox_uuid: UUID) -> _Files:
        return _Files(self.files)


REF = SandboxRef(uuid7())


def _bundle(*domains: str) -> StateBundle:
    return StateBundle.parse(
        {"cookies": [{"name": "c", "value": "v", "domain": d, "path": "/"} for d in domains]}
    )


async def test_start_waits_for_ready_then_imports_and_deletes_the_file() -> None:
    ex = _FakeExec()
    imported: list[dict] = []

    def script(argv: list[str], files: dict[str, bytes]) -> tuple[int, bytes, dict]:
        if argv[:2] == ["state", "import"]:
            imported.append(json.loads(files.pop(argv[2])))
        return 0, b'{"ok": true}\n', {}

    ex.script = script
    await DaemonClient(ex, poll_s=0).start(REF, "k3y", _bundle("taobao.com"))  # type: ignore[arg-type]
    serve, env = ex.spawned[0]
    assert "atlas-browserd serve" in serve and env["ATLAS_BROWSER_VIEW_KEY"] == "k3y"
    assert "status --wait" in ex.spawned[1][0]
    assert "state import /tmp/atlas-browser/import-" in ex.spawned[2][0]
    assert imported[0]["cookies"][0]["domain"] == "taobao.com"
    assert ex.files == {}  # 不在本地盘上留登录态


async def test_start_skips_import_when_there_is_nothing_to_import() -> None:
    ex = _FakeExec()
    await DaemonClient(ex, poll_s=0).start(REF, "k", StateBundle())  # type: ignore[arg-type]
    assert [c for c, _ in ex.spawned if "state import" in c] == []


async def test_export_and_takeover_close_take_the_result_files() -> None:
    ex = _FakeExec()

    def script(argv: list[str], files: dict[str, bytes]) -> tuple[int, bytes, dict]:
        out = argv[argv.index("--out") + 1]
        if argv[0] == "state":
            assert argv[argv.index("--sites") + 1] == "jd.com,taobao.com"
            assert argv[argv.index("--origins") + 1] == "https://www.taobao.com"
            return 0, b"{}", {out: _bundle("taobao.com").dumps()}
        body = {"domains": ["login.taobao.com", ".alipay.com"], "state": _bundle("x.cn").dump()}
        return 0, b"{}", {out: json.dumps(body).encode()}

    ex.script = script
    daemon = DaemonClient(ex, poll_s=0)  # type: ignore[arg-type]
    state = await daemon.export(
        REF, [Site("taobao.com"), Site("jd.com")], origins=["https://www.taobao.com"]
    )
    assert state.sites() == {Site("taobao.com")}
    captured = await daemon.takeover_close(REF, "t1")
    assert captured.sites() == {Site("taobao.com"), Site("alipay.com")}
    assert captured.state.sites() == {Site("x.cn")}
    assert ex.files == {}


async def test_failed_command_carries_the_reason() -> None:
    ex = _FakeExec()
    out = 'log line\n{"error": "CDP 连不上"}\n'.encode()
    ex.script = lambda argv, files: (2, out, {})
    with pytest.raises(DaemonError, match="CDP 连不上"):
        await DaemonClient(ex, poll_s=0).forget(REF, Site("a.com"))  # type: ignore[arg-type]


def test_view_key_is_derived_per_sandbox() -> None:
    keys = ViewKey(SecretStr("deploy-secret"))
    a, b = uuid7(), uuid7()
    assert keys.secret_for(a) == ViewKey(SecretStr("deploy-secret")).secret_for(a)
    assert keys.secret_for(a) != keys.secret_for(b)
    assert keys.secret_for(a) != ViewKey(SecretStr("other")).secret_for(a)
    header = keys.auth_header(a)["Authorization"]
    assert base64.b64decode(header.removeprefix("Basic ")).decode() == f"atlas:{keys.secret_for(a)}"


# ---------------------------------------------------------------------------- 附加件（库）


class _FakeDaemon:
    def __init__(self) -> None:
        self.started: list[tuple[UUID, str, StateBundle]] = []
        self.exported: list[set[Site]] = []
        self.state = StateBundle()
        self.fail_export = False

    async def start(self, ref: SandboxRef, key: str, bundle: StateBundle) -> None:
        self.started.append((ref.sandbox_uuid, key, bundle))

    async def export(self, ref: SandboxRef, sites: Any, *, origins: Any = ()) -> StateBundle:
        if self.fail_export:
            raise DaemonError("守护没了")
        self.exported.append(set(sites))
        self.origins = set(origins)
        return self.state


@pytest.fixture
async def addon(clean_db: None) -> tuple[BrowserAddon, _FakeDaemon, LoginVault, HostRegistry]:
    key = SecretStr(base64.b64encode(b"k" * 32).decode())
    vault = LoginVault(get_sessionmaker(), StateCipher(KeyRing({1: key})))
    registry = HostRegistry(get_sessionmaker())
    daemon = _FakeDaemon()
    a = BrowserAddon(daemon, vault, registry, ViewKey(SecretStr("s")))  # type: ignore[arg-type]
    return a, daemon, vault, registry


async def test_on_created_imports_logins_and_registers_the_host(
    addon: tuple[BrowserAddon, _FakeDaemon, LoginVault, HostRegistry],
) -> None:
    a, daemon, vault, registry = addon
    user, ref = uuid7(), SandboxRef(uuid7())
    await vault.save(user, _bundle("taobao.com"), sites=[Site("taobao.com")])
    assert a.applies(SandboxSpec(browser=True)) and not a.applies(SandboxSpec())
    await a.on_created(ref, SandboxSpec(browser=True), biz_type=BizType.NATIVE, user=user)
    [(sandbox, key, bundle)] = daemon.started
    assert sandbox == ref.sandbox_uuid and key == ViewKey(SecretStr("s")).secret_for(sandbox)
    assert bundle.sites() == {Site("taobao.com")}
    host = await registry.get(ref.sandbox_uuid)
    assert host is not None and host.user_uuid == user and host.kind == "native"


async def test_checkpoint_exports_only_stored_sites(
    addon: tuple[BrowserAddon, _FakeDaemon, LoginVault, HostRegistry],
) -> None:
    a, daemon, vault, registry = addon
    user, ref = uuid7(), SandboxRef(uuid7())
    await registry.register(ref, user, "native")
    assert await a.checkpoint(ref) == []  # 一个都没存过：不导出
    assert daemon.exported == []
    stored = StateBundle.parse(
        {
            "cookies": _bundle("taobao.com").dump()["cookies"],
            "origins": [{"origin": "https://www.taobao.com", "localStorage": []}],
        }
    )
    await vault.save(user, stored, sites=[Site("taobao.com")])
    daemon.state = _bundle("taobao.com", "ads.example.com")
    assert await a.checkpoint(ref) == [Site("taobao.com")]
    assert daemon.exported == [{Site("taobao.com")}]
    # 存过本地存储的源告诉守护（没开着页面它不知道）
    assert daemon.origins == {"https://www.taobao.com"}
    assert {s.site for s in await vault.sites(user)} == {"taobao.com"}
    host = await registry.get(ref.sandbox_uuid)
    assert host is not None and host.exported_at is not None


async def test_before_retire_exports_then_unregisters_even_on_failure(
    addon: tuple[BrowserAddon, _FakeDaemon, LoginVault, HostRegistry],
) -> None:
    a, daemon, vault, registry = addon
    user, ref = uuid7(), SandboxRef(uuid7())
    await registry.register(ref, user, "cli")
    await vault.save(user, _bundle("taobao.com"), sites=[Site("taobao.com")])
    daemon.fail_export = True
    await a.before_retire(ref, "idle")
    assert await registry.get(ref.sandbox_uuid) is None
    # 不是浏览器宿主：什么都不做
    await a.before_retire(SandboxRef(uuid7()), "idle")


# ---------------------------------------------------------------------------- 换算与校验


def _spec(**kw: Any) -> AgentSpec:
    kw.setdefault("tool_names", ("bash",))
    return AgentSpec(slug="a", name="a", system_prompt="", model=ModelSpec(model="m"), **kw)


def _settings(**kw: Any) -> Any:
    base = {
        "browser_state_keys": {1: SecretStr(base64.b64encode(b"k" * 32).decode())},
        "browser_view_secret": SecretStr("s"),
        "cli_default_image": "atlas-cli-bridge:1",
    }
    return get_settings().model_copy(update=base | kw)


def test_plan_switches_the_image_and_adds_the_skill() -> None:
    config = BrowserConfig(
        "atlas-sandbox-browser:1", "atlas-cli-browser:1", SkillRefSpec("browser", 2)
    )
    assert BrowserPlan.of(_spec(), config) is None
    plan = BrowserPlan.of(_spec(browser=True), config)
    assert plan is not None and plan.runtime == "native"
    sandbox = plan.sandbox(SandboxSpec())
    assert (sandbox.image, sandbox.browser) == ("atlas-sandbox-browser:1", True)
    assert plan.skills(()) == (SkillRefSpec("browser", 2),)
    pinned = (SkillRefSpec("browser", 1),)
    assert plan.skills(pinned) == pinned
    cli = BrowserPlan.of(_spec(browser=True, kind="cli", cli=CliSpec("claude-code")), config)
    assert cli is not None and (cli.runtime, cli.image) == ("cli", "atlas-cli-browser:1")


def test_plan_without_image_is_not_configured() -> None:
    from atlas_agent.browser.login import BrowserNotConfigured

    with pytest.raises(BrowserNotConfigured):
        BrowserPlan.of(_spec(browser=True), BrowserConfig())


def test_parse_skill_ref() -> None:
    assert parse_skill_ref("") is None
    assert parse_skill_ref("browser@3") == SkillRefSpec("browser", 3)
    with pytest.raises(ValueError):
        parse_skill_ref("browser")


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (_spec(browser=True, tool_names=()), "Shell"),
        (_spec(browser=True, sandbox=SandboxSpec(image="my:1")), "自定义沙箱镜像"),
        (_spec(browser=True, sandbox=SandboxSpec(profile="bench")), "评测档"),
        (
            _spec(browser=True, kind="cli", tool_names=(), cli=CliSpec("claude-code", image="x:1")),
            "自定义镜像",
        ),
    ],
)
def test_check_browser_rejects(spec: AgentSpec, message: str) -> None:
    with pytest.raises(InvalidSpec, match=message):
        check_browser(spec, _settings())


def test_check_browser_accepts_and_needs_deployment_keys() -> None:
    check_browser(_spec(browser=True), _settings())
    cli = CliSpec("claude-code", image="atlas-cli-bridge:1")
    check_browser(_spec(browser=True, kind="cli", tool_names=(), cli=cli), _settings())
    check_browser(_spec(), get_settings())  # 没开浏览器：什么都不判
    with pytest.raises(InvalidSpec, match="没有开浏览器能力"):
        check_browser(_spec(browser=True), _settings(browser_state_keys={}))


def test_device_cli_can_open_the_browser_where_there_is_a_device_gateway() -> None:
    """本机 CLI：浏览器在使用者自己的电脑上，不要登录态密钥、不换镜像、不投送技能；要设备网关。"""
    device = CliSpec("claude-code", placement="device", default_project_dir="~/p")
    spec = _spec(browser=True, kind="cli", tool_names=(), cli=device)
    gateway = {"device_gateway_url": "http://devicegw:8095"}
    check_browser(
        spec, _settings(browser_state_keys={}, browser_view_secret=SecretStr(""), **gateway)
    )
    with pytest.raises(InvalidSpec, match="没有设备网关"):
        check_browser(spec, _settings(device_gateway_url=None))
    plan = BrowserPlan.of(spec, BrowserConfig(skill=SkillRefSpec("browser", 2)))
    assert plan is not None and plan.runtime == "device" and plan.image == "" and plan.skill is None


def test_computer_use_skill_only_for_models_that_see_images() -> None:
    config = BrowserConfig(
        "atlas-sandbox-browser:1",
        skill=SkillRefSpec("browser", 2),
        computer_use_skill=SkillRefSpec("browser-computer-use", 1),
    )
    plan = BrowserPlan.of(_spec(browser=True), config)
    assert plan is not None
    assert [s.slug for s in plan.skills(())] == ["browser"]
    assert [s.slug for s in plan.skills((), vision=True)] == ["browser", "browser-computer-use"]


def test_browser_hint_tells_the_cli_which_ways_it_can_use() -> None:
    from atlas_agent.browser.plan import browser_hint

    device = BrowserPlan("device", "", None)
    both = browser_hint(device, vision=True)
    assert "用户自己的电脑上" in both and "browser-computer-use" in both and "zhipin" in both
    only = browser_hint(device, vision=False)
    assert "只能用快照" in only and "request_takeover" in only


def test_captured_sites_ignore_unparseable_domains() -> None:
    assert Captured(("", "login.taobao.com"), StateBundle()).sites() == {Site("taobao.com")}


# ---------------------------------------------------------------------------- CLI 智能体


def test_cli_pods_with_browser_share_among_themselves_and_get_the_session_name() -> None:
    from atlas_agent.sandbox.session_pod import BridgeLauncher, CliPodKind, PodRequest

    cli = CliSpec("claude-code", adapter="node acp", image="atlas-cli-browser:1")
    base = {"thread_uuid": uuid7(), "workspace_dir": "w", "user_uuid": uuid7(), "cli": cli}
    kind = CliPodKind(get_settings(), None, None)  # type: ignore[arg-type]
    plain = kind.share(PodRequest(**base))
    browser = kind.share(PodRequest(**base, browser=True))
    assert plain.key != browser.key

    req = PodRequest(**base, browser=True, agent_env={"ATLAS_BROWSER_SESSION": "s-1"})
    _, env = BridgeLauncher(get_settings(), None, None)._command(0, req)  # type: ignore[arg-type]
    assert env["ATLAS_BROWSER_SESSION"] == "s-1"
    assert "ATLAS_BROWSER_SESSION" in env["ATLAS_BRIDGE_AGENT_ENV_ALLOW"].split(",")


async def test_pod_host_switches_to_the_browser_image() -> None:
    from types import SimpleNamespace

    from atlas_agent.cli.capabilities import CliCapabilities
    from atlas_agent.cli.host import PodHost

    seen: list[tuple[Any, dict[str, Any]]] = []

    class Pods:
        async def ensure(self, thread: Any, cli: CliSpec, **kw: Any) -> Any:
            seen.append((cli, kw))
            return SimpleNamespace(url="ws://pod", token="t")

    thread = SimpleNamespace(thread_uuid=uuid7(), sandbox_thread_id=uuid7())
    cli = CliSpec("claude-code", image="atlas-cli-bridge:1")
    await PodHost(Pods()).ensure(thread, cli, CliCapabilities())  # type: ignore[arg-type]
    plan = BrowserPlan("cli", "atlas-cli-browser:1", None)
    await PodHost(Pods()).ensure(thread, cli, CliCapabilities(browser=plan))  # type: ignore[arg-type]
    (plain_cli, plain_kw), (browser_cli, browser_kw) = seen
    assert plain_cli.image == "atlas-cli-bridge:1" and "browser" not in plain_kw
    assert browser_cli.image == "atlas-cli-browser:1" and browser_kw["browser"] is True
    # 会话名 = 这个会话；屏幕的那一块按对话（根会话）分
    assert browser_kw["agent_env"] == {
        "ATLAS_BROWSER_SESSION": str(thread.thread_uuid),
        "ATLAS_BROWSER_SCREEN": str(thread.sandbox_thread_id),
    }
