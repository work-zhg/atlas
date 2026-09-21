"""会话标题自动生成（文档 §8，决策 5）。

时机由 engine 决定：主输出流结束后、`run.finished` 之前调用本模块，
产出 `thread.title_generated` 事件。SSE 在 run.finished 后就关闭了，
异步生成的标题送不出去，所以必须挤进这个窗口。

haiku 生成 20 字标题通常 <1s，而此刻用户正在读刚输出完的内容，
感知不到这点延迟。这样只有一条通道，前端不需要额外轮询或 WebSocket。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from atlas_server.domain.spec import ModelSpec
from atlas_server.domain.translator import extract_text
from langchain_core.messages import HumanMessage

from ..config import Settings

if TYPE_CHECKING:
    from ..executor.assembly import ModelBuilder

logger = logging.getLogger(__name__)

#: §8.2：≤20 个中文字符，名词短语，不带标点，不带虚词
_PROMPT = """为下面这段对话起一个标题。

要求：
- 不超过 20 个字
- 名词短语，不要动词开头
- 不要标点符号
- 不要"关于""如何""讨论"这类虚词
- 直接输出标题本身，不要任何解释

用户问：
{question}

助手答（节选）：
{answer}"""

#: 助手回复只取前 500 字 —— 标题取决于主题，不取决于细节，
#: 全文喂进去只是徒增 token 与延迟。
_ANSWER_LIMIT = 500
#: 降级时截断用户首条消息的长度（§8.2）
_FALLBACK_LIMIT = 24


#: §8.2 的「20 个中文字符」按**视觉宽度**算，不是按字符数。
#: 实测教训：'Python GIL全局解释器锁原理与限制' 只有 16 个汉字宽，
#: 但字符数是 22，按字符截断会砍成 '…原理与限'，末尾是半个词。
_TITLE_WIDTH = 20


def _width(ch: str) -> float:
    """CJK 及全角标点算 1，其余算 0.5。"""
    code = ord(ch)
    is_wide = (
        0x4E00 <= code <= 0x9FFF  # CJK 统一表意
        or 0x3400 <= code <= 0x4DBF  # 扩展 A
        or 0x3000 <= code <= 0x303F  # CJK 标点
        or 0xFF00 <= code <= 0xFF60  # 全角
        or 0xAC00 <= code <= 0xD7AF  # 谚文
        or 0x3040 <= code <= 0x30FF  # 假名
    )
    return 1.0 if is_wide else 0.5


def _truncate(text: str, budget: float = _TITLE_WIDTH) -> str:
    used = 0.0
    for i, ch in enumerate(text):
        used += _width(ch)
        if used > budget:
            return text[:i]
    return text


def _clean(raw: str) -> str:
    """模型偶尔会带引号或句号，去掉；再按视觉宽度收口。"""
    text = raw.strip().strip("“”\"'《》「」").rstrip("。.！!？?").strip()
    return _truncate(text)


def fallback_title(question: str) -> str:
    """降级：用户首条消息截断（§8.2）。"""
    text = " ".join(question.split())
    return text[:_FALLBACK_LIMIT] or "新会话"


class TitleService:
    """生成标题。**不碰数据库** —— 落库由执行器在收尾时统一做。

    ★ 模型经 `model_builder` 注入，与执行器共用同一个接缝。
      自己调 build_chat_model 会绕过它：单测注入的假模型对标题生成无效，
      于是每个 run 都真去打网关、超时几秒再降级。踩过一次。
    """

    def __init__(self, settings: Settings, model_builder: ModelBuilder) -> None:
        self._settings = settings
        self._build_model = model_builder

    async def generate(self, question: str, answer: str) -> dict[str, Any]:
        """返回 `thread.title_generated` 的 data。

        永不抛异常：标题生成失败只是标题难看一点，不该影响这轮对话。
        """
        try:
            async with asyncio.timeout(self._settings.titling_timeout_s):
                title = await self._ask_model(question, answer)
            if title:
                return {"title": title, "degraded": False}
        except TimeoutError:
            logger.info("标题生成超时（%.1fs），降级", self._settings.titling_timeout_s)
        except Exception:
            logger.warning("标题生成失败，降级", exc_info=True)

        return {"title": fallback_title(question), "degraded": True}

    async def _ask_model(self, question: str, answer: str) -> str:
        # ★ 固定用 summarizer_model（haiku），不跟随 agent 配置：
        #   用 opus 生成 20 字标题是纯浪费，且 opus 的 effort 参数在这里毫无意义。
        chat = self._build_model(
            ModelSpec(model=self._settings.summarizer_model, max_output_tokens=64),
            base_url=str(self._settings.litellm_base_url),
            api_key=self._settings.litellm_key.get_secret_value(),
        )
        prompt = _PROMPT.format(question=question, answer=answer[:_ANSWER_LIMIT])
        result = await chat.ainvoke([HumanMessage(content=prompt)])
        # ★ 必须走 extract_text 而不是 str(content)：模型返回 content blocks 时
        #   （DeepSeek 的 Anthropic 兼容层就是如此，块里还带 signature），
        #   str() 出来的是 Python repr，会被当成标题原样写进 thread.title。
        return _clean(extract_text(result.content))
