"""供应商能写成数据的部分（c4-model/modelgateway/code-gateway-provider.html）。

认证方式、各格式的默认地址、查余额和拉模型列表的接口都是数据；只有字段改名、用量换算、
报错归类这类逻辑才写方言（dialect.py）。Azure、Gemini 只靠数据就能接。

★ 这里的地址是「添加账号时的默认值」，账号上登记的地址才是真正用的。
★ 各家具体的写法以各家文档和实测为准；新接一家先只加一条数据，发现和标准不一样再加方言。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from ..shared.kernel import Format

__all__ = ["PROVIDERS", "AuthStyle", "ProviderSpec"]


class AuthStyle(StrEnum):
    #: Authorization: Bearer <密钥>
    BEARER = "bearer"
    #: x-api-key: <密钥>，再加 anthropic-version（Anthropic 官方）
    X_API_KEY = "x_api_key"
    #: api-key: <密钥>（Azure）
    API_KEY_HEADER = "api_key_header"
    #: x-goog-api-key: <密钥>（Gemini 原生接口）
    GOOG_API_KEY = "goog_api_key"


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    display_name: str
    #: 提供哪些格式，第一个是推荐的（两种都有时推荐 Anthropic：网关原样转发，不丢思考和缓存标记）
    formats: tuple[Format, ...]
    #: 各格式的默认地址；空串 = 必须手填。地址里可以带 {model}（Azure 的部署名就是它）
    endpoints: Mapping[Format, str] = field(default_factory=dict)
    #: 各格式的认证方式；没写的格式按 Bearer
    auth: Mapping[Format, AuthStyle] = field(default_factory=dict)
    #: 查余额的完整地址和余额在返回体里的位置（点分路径，数字是数组下标）；没有 = 不支持
    balance_url: str | None = None
    balance_path: str | None = None
    #: 能不能拉模型列表（各格式的标准接口）
    list_models: bool = True
    #: 上游模型名的填写提示
    model_hint: str = ""
    #: 密钥可以不填（自部署）
    key_optional: bool = False
    #: 海外供应商：默认经出网代理
    overseas: bool = False

    def auth_style(self, fmt: Format) -> AuthStyle:
        return self.auth.get(fmt, AuthStyle.BEARER)

    @property
    def needs_endpoint(self) -> bool:
        return not any(self.endpoints.get(f) for f in self.formats)


_A, _O, _G = Format.ANTHROPIC, Format.OPENAI, Format.GOOGLE

PROVIDERS: dict[str, ProviderSpec] = {
    s.name: s
    for s in (
        ProviderSpec(
            "deepseek",
            "DeepSeek",
            (_A, _O),
            {_A: "https://api.deepseek.com/anthropic", _O: "https://api.deepseek.com"},
            balance_url="https://api.deepseek.com/user/balance",
            balance_path="balance_infos.0.total_balance",
        ),
        ProviderSpec(
            "anthropic",
            "Anthropic",
            (_A,),
            {_A: "https://api.anthropic.com"},
            {_A: AuthStyle.X_API_KEY},
            overseas=True,
        ),
        ProviderSpec("openai", "OpenAI", (_O,), {_O: "https://api.openai.com/v1"}, overseas=True),
        ProviderSpec(
            "gemini",
            "Google Gemini",
            (_G,),
            {_G: "https://generativelanguage.googleapis.com/v1beta"},
            {_G: AuthStyle.GOOG_API_KEY},
            overseas=True,
        ),
        ProviderSpec(
            "azure",
            "Azure OpenAI",
            (_O,),
            {_O: ""},
            {_O: AuthStyle.API_KEY_HEADER},
            model_hint=(
                "地址填 https://<资源名>.openai.azure.com/openai/deployments/{model}"
                "?api-version=<API 版本>，上游模型名填部署名"
            ),
        ),
        ProviderSpec(
            "dashscope",
            "通义千问（百炼）",
            (_O,),
            {_O: "https://dashscope.aliyuncs.com/compatible-mode/v1"},
        ),
        ProviderSpec(
            "volcengine",
            "火山方舟",
            (_O,),
            {_O: "https://ark.cn-beijing.volces.com/api/v3"},
            list_models=False,
            model_hint="火山方舟按接入点调用：上游模型名填接入点 ID（ep-…）",
        ),
        ProviderSpec(
            "moonshot",
            "Moonshot（Kimi）",
            (_O, _A),
            {_O: "https://api.moonshot.cn/v1", _A: "https://api.moonshot.cn/anthropic"},
            balance_url="https://api.moonshot.cn/v1/users/me/balance",
            balance_path="data.available_balance",
        ),
        ProviderSpec(
            "minimax",
            "MiniMax",
            (_A, _O),
            {_A: "https://api.minimax.cn/anthropic", _O: "https://api.minimax.cn/v1"},
            list_models=False,
            model_hint="国内站；国际站是 https://api.minimax.io/anthropic",
        ),
        ProviderSpec(
            "zhipu",
            "智谱 GLM",
            (_O, _A),
            {
                _O: "https://open.bigmodel.cn/api/paas/v4",
                _A: "https://open.bigmodel.cn/api/anthropic",
            },
        ),
        ProviderSpec(
            "siliconflow",
            "硅基流动",
            (_O,),
            {_O: "https://api.siliconflow.cn/v1"},
            balance_url="https://api.siliconflow.cn/v1/user/info",
            balance_path="data.totalBalance",
        ),
        ProviderSpec("vllm", "vLLM 自部署", (_O,), {_O: ""}, key_optional=True),
        ProviderSpec("custom_openai", "其他（OpenAI 兼容）", (_O,), {_O: ""}),
        ProviderSpec(
            "custom_anthropic", "其他（Anthropic 兼容）", (_A,), {_A: ""}, list_models=False
        ),
    )
}
