"""浏览器 · 宿主：一个智能体版本的浏览器配置换算（c4-model/agent/browser/code-browser-host.html）。

    BrowserConfig   部署给的浏览器镜像和浏览器技能（运行期配置：管理员改了，
                    下一个新建的沙箱就用新的）
    BrowserPlan     换算结果：哪种运行时、用哪个镜像、带哪些浏览器技能
    check_browser   保存智能体时判能不能开：要 Shell、不能配自定义镜像；本机 CLI 要部署了设备网关
    browser_hint    交给 CLI 的系统提示追加：这次能用哪几种操作方式（模型能不能看图）

★ 单独一个模块、只依赖引擎的 spec：运行期配置要用它，而宿主的其余部分依赖沙箱组件。
★ 开浏览器 = 换一个带浏览器的镜像，不装进默认镜像：Chromium 加中文字体几百 MB，大多数智能体用不上；
  运行时现装太慢，在构建镜像时装好。换了镜像，沙箱按（模板，镜像）共享的规则自然把带浏览器
  的沙箱和普通沙箱分开。
★ 本机 CLI 智能体（runtime = device）：浏览器在使用者自己的电脑上，不换镜像；浏览器组件和两个技能
  由电脑主人装（atlas-connect browser install），平台不投送技能（design-browser-device.html）。
★ 两种操作方式各一个技能：快照（默认，纯文本模型也能用）、computer use（要能看图）。模型平台登记
  模型时标注能不能看图，不能看图的只给快照。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from atlas_engine.contracts import InvalidSpec
from atlas_engine.contracts.spec import AgentSpec, SandboxSpec, SkillRefSpec
from atlas_engine.runtime.tool_registry import BASH_TOOL

from .login import BrowserNotConfigured

if TYPE_CHECKING:
    from ..config import Settings

__all__ = ["BrowserConfig", "BrowserPlan", "browser_hint", "check_browser", "parse_skill_ref"]


def parse_skill_ref(raw: str) -> SkillRefSpec | None:
    """「slug@版本」→ SkillRefSpec；空串 = 不带技能。格式不对抛 ValueError。"""
    raw = raw.strip()
    if not raw:
        return None
    slug, sep, version = raw.rpartition("@")
    if not sep or not slug or not version.isdigit():
        raise ValueError(f"浏览器技能要写成 slug@版本（如 browser@1）：{raw!r}")
    return SkillRefSpec(slug=slug, version=int(version))


@dataclass(frozen=True)
class BrowserConfig:
    """部署给的浏览器镜像和技能。空镜像 = 这种运行时还没有浏览器镜像。"""

    sandbox_image: str = ""
    cli_image: str = ""
    skill: SkillRefSpec | None = None
    #: computer use 技能（模型能看图才带上）
    computer_use_skill: SkillRefSpec | None = None


@dataclass(frozen=True)
class BrowserPlan:
    """一个智能体版本的浏览器配置换算结果。"""

    runtime: Literal["native", "cli", "device"]
    image: str
    skill: SkillRefSpec | None
    computer_use_skill: SkillRefSpec | None = None

    @classmethod
    def of(cls, spec: AgentSpec, config: BrowserConfig) -> BrowserPlan | None:
        """没开浏览器返回 None；开了却没有对应的镜像抛 BrowserNotConfigured（不静默少一个能力）。"""
        if not spec.browser:
            return None
        if spec.kind == "cli" and spec.cli is not None and spec.cli.on_device:
            # 电脑上的浏览器：不换镜像，技能随浏览器组件装在电脑上
            return cls(runtime="device", image="", skill=None)
        runtime: Literal["native", "cli"] = "cli" if spec.kind == "cli" else "native"
        image = config.cli_image if runtime == "cli" else config.sandbox_image
        if not image:
            which = "浏览器 CLI 镜像" if runtime == "cli" else "浏览器沙箱镜像"
            raise BrowserNotConfigured(f"这个部署还没有配置{which}，浏览器能力用不了")
        return cls(
            runtime=runtime,
            image=image,
            skill=config.skill,
            computer_use_skill=config.computer_use_skill,
        )

    def sandbox(self, spec: SandboxSpec) -> SandboxSpec:
        """原生沙箱的规格：换成浏览器沙箱镜像、带 browser 属性（附加件看它，不看镜像名）。"""
        return replace(spec, image=self.image, browser=True)

    def skills(
        self, skills: tuple[SkillRefSpec, ...], *, vision: bool = False
    ) -> tuple[SkillRefSpec, ...]:
        """作者配的技能加上浏览器技能（作者自己钉了同名技能的，用作者的）；模型能看图再加上
        computer use 技能。"""
        extra = [self.skill, self.computer_use_skill if vision else None]
        out = list(skills)
        for skill in extra:
            if skill is not None and all(s.slug != skill.slug for s in out):
                out.append(skill)
        return tuple(out)


#: 交给 CLI 的系统提示追加（会话开始时）：浏览器技能装在电脑上（本机）或拷进会话（Pod），
#: 这里只说这次能用哪几种
_HINT = """## 浏览器
你可以用 Shell 里的 browser 命令操作一个浏览器（{where}）。{ways}
遇到登录、验证码、扫码，调用 request_takeover 请用户接管，不要自己输密码。"""


def browser_hint(plan: BrowserPlan, *, vision: bool) -> str:
    where = "在用户自己的电脑上" if plan.runtime == "device" else "在平台的沙箱里"
    if vision:
        ways = (
            "这次两种操作方式都能用：快照（技能 browser，读页面清单、按编号点，普通网页用它）和 "
            "computer use（技能 browser-computer-use，看截图、用系统键鼠）。会检测自动化的网站"
            "（如 zhipin.com）只用 computer use，从头到尾别用快照的命令。"
        )
    else:
        ways = (
            "这次只能用快照方式（技能 browser）：当前模型不能看图，不要用 browser look 这类 "
            "computer use 命令。"
        )
    return _HINT.format(where=where, ways=ways)


def check_browser(spec: AgentSpec, settings: Settings) -> None:
    """保存时判能不能开浏览器，不等到跑起来才报「浏览器启动失败」。"""
    if not spec.browser:
        return
    if spec.kind == "cli" and spec.cli is not None and spec.cli.on_device:
        # 本机 CLI：浏览器在使用者自己的电脑上，画面经设备网关；电脑没装组件时开会话再提示
        if not settings.device_gateway_url:
            raise InvalidSpec("这个部署没有设备网关，本机 CLI 智能体不能开浏览器")
        return
    if not settings.browser_state_keys or not settings.browser_view_secret.get_secret_value():
        raise InvalidSpec(
            "这个部署没有开浏览器能力（没有配置登录态密钥 BROWSER_STATE_KEYS、"
            "画面密钥 BROWSER_VIEW_SECRET）"
        )
    if spec.kind == "cli":
        cli = spec.cli
        if cli is not None and cli.image and cli.image != settings.cli_default_image:
            raise InvalidSpec(
                "配了自定义镜像的智能体不能开浏览器：浏览器在平台的浏览器 CLI 镜像里",
                image=cli.image,
            )
        return
    if BASH_TOOL not in spec.tool_names:
        raise InvalidSpec("开浏览器要勾选 Shell：浏览器命令在沙箱里执行")
    if spec.sandbox.image:
        raise InvalidSpec(
            "配了自定义沙箱镜像的智能体不能开浏览器：浏览器在平台的浏览器沙箱镜像里",
            image=spec.sandbox.image,
        )
    if spec.sandbox.profile == "bench":
        raise InvalidSpec("评测档沙箱不能开浏览器")
