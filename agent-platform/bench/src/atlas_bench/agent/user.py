"""模拟用户：Agent 用 ask_user_question 提问时，按任务里「用户心里的需求」作答。

设计：doc/ask-user-question-design.html §12。

★ 只依据 brief 回答，不替 Agent 补全需求：需求里没提到的方面选推荐项
  （没有推荐就跳过，交给 Agent 定）。这样「问到了关键维度」才拿得到答案，问偏了就拿不到 ——
  问题质量会反映在结果上。
★ 回答收拾成服务端接受的形状再提交（只留存在的选项，单选最多一个、选了选项就不填「其他」）：
  模型偶尔多写一个选项，不该让一次 trial 因为 400 卡住。
"""

from __future__ import annotations

import json
from typing import Any

from ..llm import LlmClient

__all__ = ["SimulatedUserAnswerer", "normalize_answer"]

_PROMPT = """你在扮演一位向智能体提需求的用户。你心里的完整需求如下（智能体看不到）：

{brief}

智能体为了弄清需求，向你提了几个问题（JSON）。请只依据上面的需求、只就这个问题本身作答：
- 单选题：有选项完全符合就选它（label 原样照抄）；只部分符合（你的需求比它多出
  与这个问题相关的关键要点）或都不符合时，不选选项，在 other 里用一句话写全你的需求
- 多选题（multi_select 为 true）：选出所有符合的选项；与这个问题相关、但选项没覆盖的要点写进 other
- 选项只给了类别或泛称（如「核心指标卡」），而你的需求里有与这个问题相关的具体内容
  （具体是哪些指标、哪些环节、哪些功能）时：单选题不选选项、在 other 里写全；
  多选题选上符合的选项，并把具体内容写进 other
- 需求里没提到的方面，选标了 recommended 的那个；没有推荐项就 selected 留空、other 为 null
- other 不超过 100 字；单选题 selected 最多一个，选了选项就不要再写 other

只输出 JSON：{{"answers": [{{"selected": ["…"], "other": null}}]}}，条数、顺序与问题一致。

问题：
{questions}"""


def normalize_answer(question: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    """一题的回答 → 服务端接受的形状（kernel ask_user.resolve_answers 的校验规则）。"""
    labels = [o.get("label") for o in question.get("options") or []]
    picked = answer.get("selected") or []
    selected = [s for s in dict.fromkeys(picked) if s in labels]
    other = str(answer.get("other") or "").strip()[:500] or None
    if not question.get("multi_select") and selected:
        selected, other = selected[:1], None
    return {"selected": selected, "other": other}


class SimulatedUserAnswerer:
    def __init__(self, llm: LlmClient, model: str) -> None:
        self._llm = llm
        self._model = model

    async def answer(self, brief: str, questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prompt = _PROMPT.format(
            brief=brief.strip(), questions=json.dumps(questions, ensure_ascii=False, indent=1)
        )
        # ★ max_tokens 留足：推理模型（deepseek-v4-pro）的思考也算在里面，1024 时 4 道题会把预算
        #   全花在思考上、正文为空（112 上实测）
        out = await self._llm.chat_json(
            self._model, [{"role": "user", "content": prompt}], max_tokens=4096
        )
        raw = out.get("answers") or []
        return [
            normalize_answer(q, raw[i] if i < len(raw) and isinstance(raw[i], dict) else {})
            for i, q in enumerate(questions)
        ]
