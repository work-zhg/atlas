"""供应商的唯一入口：按名字给出数据和方言。网关和管理平台用同一个包。"""

from __future__ import annotations

from collections.abc import Mapping

from ..gateway.kernel import Format
from .dialect import ProviderDialect, StandardDialect
from .dialects import DIALECTS
from .spec import PROVIDERS, ProviderSpec

__all__ = ["ProviderCatalog"]


class ProviderCatalog:
    def __init__(
        self,
        specs: Mapping[str, ProviderSpec] = PROVIDERS,
        dialects: Mapping[str, type[StandardDialect]] = DIALECTS,
    ) -> None:
        self._specs = dict(specs)
        self._classes = dict(dialects)
        self._dialects: dict[str, ProviderDialect] = {}

    def providers(self) -> list[ProviderSpec]:
        return list(self._specs.values())

    def known(self, name: str) -> bool:
        return name in self._specs

    def spec(self, name: str) -> ProviderSpec:
        """不认识的供应商（比如旧数据里 LiteLLM 的写法）按「两种格式都可能、标准写法」处理。"""
        found = self._specs.get(name)
        if found is not None:
            return found
        return ProviderSpec(name, name, (Format.ANTHROPIC, Format.OPENAI))

    def dialect(self, name: str) -> ProviderDialect:
        found = self._dialects.get(name)
        if found is None:
            cls = self._classes.get(name, StandardDialect)
            found = self._dialects[name] = cls(self.spec(name))
        return found
