"""P5 · §8 会话标题自动生成（决策 5）—— server 侧链路。

engine 侧的时序（标题必须在 run.finished 之前发出）见 test_subagents.py。
这里测的是落库行为：谁会被写、谁不会被覆盖。
"""

from __future__ import annotations

import httpx
import pytest
from atlas_server.config import get_settings
from atlas_server.db.session import get_sessionmaker
from atlas_server.executor.inprocess import InProcessExecutor
from atlas_server.main import create_app
from atlas_server.services.title import _clean, fallback_title
from httpx import ASGITransport

from tests.fakes import text_model
from tests.test_runs_api import wait_for_status


def make_app(reply: str = "生成的标题"):
    """假模型同时承担「回答」与「起标题」两个角色 ——
    TitleService 与执行器共用 model_builder，注入一次即可。"""
    app = create_app()
    app.state.executor = InProcessExecutor(
        get_sessionmaker(),
        get_settings(),
        model_builder=lambda *_a, **_k: text_model(reply),
    )
    return app


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = ASGITransport(app=make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _thread(client: httpx.AsyncClient, *, title: str = "") -> str:
    agent = await client.post(
        "/v1/agents",
        json={
            "slug": "titler-test",
            "name": "标题测试",
            "spec": {"system_prompt": "p", "model": {"model": "claude-opus-5"}},
        },
    )
    assert agent.status_code == 201, agent.text
    thread = await client.post("/v1/threads", json={"agent_id": agent.json()["id"], "title": title})
    return thread.json()["id"]


async def _run_once(client: httpx.AsyncClient, thread_id: str) -> None:
    res = await client.post(
        f"/v1/threads/{thread_id}/runs",
        json={"content": [{"type": "text", "text": "介绍一下 Atlas 的分层设计"}]},
    )
    assert res.status_code == 202, res.text
    await wait_for_status(client, res.json()["run_id"])


@pytest.mark.usefixtures("clean_db")
async def test_new_thread_gets_generated_title(client: httpx.AsyncClient) -> None:
    """★ P5 完成标准：新会话自动有标题。"""
    thread_id = await _thread(client)
    before = (await client.get(f"/v1/threads/{thread_id}")).json()
    assert before["title_source"] == "pending"

    await _run_once(client, thread_id)

    after = (await client.get(f"/v1/threads/{thread_id}")).json()
    # 断言等于假模型的输出而不是「非空」—— 后者在标题生成完全失败、
    # 一路降级成截断用户输入时同样成立，测不出模型路径通没通。
    assert after["title"] == "生成的标题"
    assert after["title_source"] == "generated"


@pytest.mark.usefixtures("clean_db")
async def test_manual_title_is_never_overwritten(client: httpx.AsyncClient) -> None:
    """★ 决策 5 的核心：用户改过的标题被自动生成冲掉是很恼人的 bug。"""
    thread_id = await _thread(client, title="我自己起的名字")
    created = (await client.get(f"/v1/threads/{thread_id}")).json()
    assert created["title_source"] == "manual"

    await _run_once(client, thread_id)

    after = (await client.get(f"/v1/threads/{thread_id}")).json()
    assert after["title"] == "我自己起的名字"
    assert after["title_source"] == "manual"


@pytest.mark.usefixtures("clean_db")
async def test_title_generated_only_once(client: httpx.AsyncClient) -> None:
    """已有标题的会话不该每轮都重新生成 —— 既费钱又会来回变。"""
    thread_id = await _thread(client)
    await _run_once(client, thread_id)
    first = (await client.get(f"/v1/threads/{thread_id}")).json()["title"]

    # 第二轮：title_source 已不是 pending，titler 应当返回 None
    await _run_once(client, thread_id)
    second = (await client.get(f"/v1/threads/{thread_id}")).json()["title"]

    assert first == second


# ---------------------------------------------------------------- 纯函数


def test_clean_strips_quotes_and_trailing_punctuation() -> None:
    """模型偶尔会带引号或句号。"""
    assert _clean('  "Atlas 分层设计"  ') == "Atlas 分层设计"
    assert _clean("Atlas 分层设计。") == "Atlas 分层设计"
    assert _clean("「Atlas 分层设计」") == "Atlas 分层设计"


def test_clean_caps_at_twenty_cjk_widths() -> None:
    """§8.2 的「20 个中文字符」是视觉宽度，不是字符数。"""
    assert len(_clean("一" * 50)) == 20

    # ★ 实测回归：这个标题字符数 22，按字符截断会砍成半个词（'…原理与限'），
    #   按视觉宽度只有 16，应当完整保留。
    real = "Python GIL全局解释器锁原理与限制"
    assert _clean(real) == real

    # 英文标题不该被 20 字符卡住 —— 40 个拉丁字符才等于 20 个汉字宽
    latin = "a" * 40
    assert _clean(latin) == latin
    assert _clean("a" * 41) == "a" * 40


def test_fallback_truncates_first_message() -> None:
    """§8.2：降级用用户首条消息截断至 24 字。"""
    assert fallback_title("一" * 100) == "一" * 24
    assert fallback_title("") == "新会话"
    # 多余空白要压掉，否则标题里全是换行
    assert fallback_title("介绍一下\n\n  Atlas") == "介绍一下 Atlas"
