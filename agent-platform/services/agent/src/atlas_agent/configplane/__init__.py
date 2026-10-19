"""运行时对配置服务（atlas-config）的只读消费（doc/skill-mcp-backend-design.html §7.1）。

调用方向只有 server → config。类型直接用 `atlas_config.schemas` —— 那是 server
唯一允许 import 的配置服务模块（import-linter）。
"""

from .client import ConfigPlaneUnavailable
from .skills import (
    FakeSkillDirectory,
    HttpSkillDirectory,
    SkillDirectory,
    UnconfiguredSkillDirectory,
    override_skill_directory,
    skill_directory,
)

__all__ = [
    "ConfigPlaneUnavailable",
    "FakeSkillDirectory",
    "HttpSkillDirectory",
    "SkillDirectory",
    "UnconfiguredSkillDirectory",
    "override_skill_directory",
    "skill_directory",
]
