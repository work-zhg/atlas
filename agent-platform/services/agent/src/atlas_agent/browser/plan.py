"""浏览器 · 宿主：一个智能体版本的浏览器配置换算（c4-model/agent/browser/code-browser-host.html）。

    BrowserConfig   部署给的浏览器镜像和浏览器技能（运行期配置：管理员改了，
                    下一个新建的沙箱就用新的）
    BrowserPlan     换算结果：哪种运行时、用哪个镜像、带哪个浏览器技能
    check_browser   保存智能体时判能不能开：要 Shell、不能配自定义镜像、本机 CLI 不行

★ 单独一个模块、只依赖引擎的 spec：运行期配置要用它，而宿主的其余部分依赖沙箱组件。
★ 开浏览器 = 换一个带浏览器的镜像，不装进默认镜像：Chromium 加中文字体几百 MB，大多数智能体用不上；
  strict 档根目录只读，只能在构建镜像时装好。换了镜像，沙箱按（模板，镜像）共享的规则自然把带浏览器
  的沙箱和普通沙箱分开。
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

__all__ = ["BrowserConfig", "BrowserPlan", "check_browser", "parse_skill_ref"]


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


@dataclass(frozen=True)
class BrowserPlan:
    """一个智能体版本的浏览器配置换算结果。"""

    runtime: Literal["native", "cli"]
    image: str
    skill: SkillRefSpec | None

    @classmethod
    def of(cls, spec: AgentSpec, config: BrowserConfig) -> BrowserPlan | None:
        """没开浏览器返回 None；开了却没有对应的镜像抛 BrowserNotConfigured（不静默少一个能力）。"""
        if not spec.browser:
            return None
        runtime: Literal["native", "cli"] = "cli" if spec.kind == "cli" else "native"
        image = config.cli_image if runtime == "cli" else config.sandbox_image
        if not image:
            which = "浏览器 CLI 镜像" if runtime == "cli" else "浏览器沙箱镜像"
            raise BrowserNotConfigured(f"这个部署还没有配置{which}，浏览器能力用不了")
        return cls(runtime=runtime, image=image, skill=config.skill)

    def sandbox(self, spec: SandboxSpec) -> SandboxSpec:
        """原生沙箱的规格：换成浏览器沙箱镜像、带 browser 属性（附加件看它，不看镜像名）。"""
        return replace(spec, image=self.image, browser=True)

    def skills(self, skills: tuple[SkillRefSpec, ...]) -> tuple[SkillRefSpec, ...]:
        """作者配的技能加上浏览器技能（作者自己钉了同名技能的，用作者的）。"""
        if self.skill is None or any(s.slug == self.skill.slug for s in skills):
            return skills
        return (*skills, self.skill)


def check_browser(spec: AgentSpec, settings: Settings) -> None:
    """保存时判能不能开浏览器，不等到跑起来才报「浏览器启动失败」。"""
    if not spec.browser:
        return
    if not settings.browser_state_keys or not settings.browser_view_secret.get_secret_value():
        raise InvalidSpec(
            "这个部署没有开浏览器能力（没有配置登录态密钥 BROWSER_STATE_KEYS、"
            "画面密钥 BROWSER_VIEW_SECRET）"
        )
    if spec.kind == "cli":
        cli = spec.cli
        if cli is None or cli.on_device:
            raise InvalidSpec(
                "本机 CLI 智能体不能开浏览器："
                "浏览器在平台的会话 Pod 里，本机 CLI 用的是你自己的电脑"
            )
        if cli.image and cli.image != settings.cli_default_image:
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
