"""atlas_acp 线协议包的契约与边界（acp 详设 §03 / §05）。

两类断言：
  · **包边界** —— atlas_acp 会装进 Pod 镜像，必须能脱离整个 atlas 依赖树
    独立安装。这条破了，每个会话 Pod 都会背上 sqlalchemy/fastapi。
  · **帧与能力位** —— server 与 bridge 用同一份模型解析，漂移在类型层暴露。
"""

from __future__ import annotations

import subprocess
import sys

from atlas_acp.caps import AgentCaps
from atlas_acp.types import STOP_REASONS, RequestPermissionResult, SessionPromptResult
from atlas_acp.updates import SessionUpdateNotification
from atlas_acp.wire import (
    ErrorCode,
    Method,
    Notification,
    Request,
    Response,
    is_notification,
    parse_frame,
)

#: 装进 Pod 就等于每个会话多背一层的东西。
_FORBIDDEN = (
    "atlas_server",
    "atlas_engine",
    "sqlalchemy",
    "fastapi",
    "redis",
    "langchain",
    "langchain_core",
    "langgraph",
    "boto3",
)


# ──────────────────────────────────────────────── 包边界


def test_acp_package_is_a_leaf() -> None:
    """★ import atlas_acp 不得拖进本仓其它包或任何重依赖。

    bridge 依赖它并被打进 Pod 镜像 —— 多一个依赖就是每个会话 Pod 多一层
    字节与一个 CVE 面。import-linter 有同名 contract（CI），这里在运行时
    兜一层：子进程干净 import，检查 sys.modules。
    """
    modules = ("wire", "types", "updates", "caps")
    code = (
        "import sys, importlib;"
        f"[importlib.import_module('atlas_acp.' + m) for m in {modules!r}];"
        f"bad=[m for m in {_FORBIDDEN!r} if m in sys.modules];"
        "print(','.join(bad))"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    leaked = proc.stdout.strip()
    assert leaked == "", f"atlas_acp 泄漏了重依赖：{leaked}"


def test_method_surface_stays_small() -> None:
    """方法面刻意收敛（§05）。加一个之前先问：能不能用现有方法的参数表达？

    Pod 在跑时没法原地升级 —— 协议面每宽一寸，bridge 与 server 的版本
    耦合就紧一分。
    """
    names = {v for k, v in vars(Method).items() if not k.startswith("_") and isinstance(v, str)}
    assert names == {
        "initialize",
        "session/new",
        "session/load",
        "session/prompt",
        "session/cancel",
        "session/update",
        "session/request_permission",
        "bridge/adapter_crashed",
    }


# ──────────────────────────────────────────────── 帧


def test_notification_is_distinguished_by_absent_id() -> None:
    """通知与请求的唯一区别是有没有 id。

    分错的后果：给通知回响应，对端的请求-响应关联表里多出对不上号的条目。
    """
    assert is_notification({"method": "session/update", "params": {}})
    assert not is_notification({"id": 1, "method": "initialize"})

    assert isinstance(parse_frame({"method": Method.SESSION_UPDATE, "params": {}}), Notification)
    assert isinstance(parse_frame({"id": 7, "method": Method.INITIALIZE}), Request)
    assert isinstance(parse_frame({"id": 7, "result": {}}), Response)


def test_response_ok_reflects_error_presence() -> None:
    assert parse_frame({"id": 1, "result": {"sessionId": "s"}}).ok
    bad = parse_frame({"id": 1, "error": {"code": ErrorCode.SESSION_BUSY, "message": "busy"}})
    assert not bad.ok
    assert bad.error.code == ErrorCode.SESSION_BUSY


def test_unknown_fields_survive_a_roundtrip() -> None:
    """协议演进时，旧的一端遇到新字段不该把连接弄死。"""
    frame = parse_frame({"id": 1, "method": "initialize", "params": {}, "futureField": 42})
    assert frame.model_dump()["futureField"] == 42


# ──────────────────────────────────────────────── 能力位


def test_resume_needs_both_capability_bits() -> None:
    """★ 只看 loadSession 不够。

    有的 adapter 实现了方法但会话本身不可恢复。缺任一个都要走 lost 降级 ——
    「以为在继续、实际从零开始」是最坏的失败形态（Subagent §04）。
    """
    assert AgentCaps().can_resume is False
    assert AgentCaps(loadSession=True).can_resume is False
    assert AgentCaps(session={"resume": True}).can_resume is False
    assert AgentCaps(loadSession=True, session={"resume": True}).can_resume is True


def test_capabilities_default_closed() -> None:
    """没声明的能力一律当作没有 —— 默认 True 会让错误发生在运行中而不是握手时。

    fs.* 尤其：打开 writeTextFile 等于允许 CLI 绕过审批回路直接改宿主文件。
    """
    caps = AgentCaps()
    assert caps.load_session is False
    assert caps.fs.read_text_file is False
    assert caps.fs.write_text_file is False
    assert caps.terminal is False


def test_caps_parse_from_camel_case_wire_form() -> None:
    caps = AgentCaps(**{"loadSession": True, "fs": {"readTextFile": True}, "terminal": True})
    assert caps.load_session and caps.fs.read_text_file and caps.terminal


# ──────────────────────────────────────────────── 方法载荷


def test_stop_reasons_are_the_five_documented_ones() -> None:
    assert STOP_REASONS == {
        "end_turn",
        "max_tokens",
        "max_turn_requests",
        "refusal",
        "cancelled",
    }


def test_prompt_result_parses_camel_case_usage() -> None:
    result = SessionPromptResult(
        **{"stopReason": "end_turn", "usage": {"inputTokens": 10, "totalTokens": 15}}
    )
    assert result.stop_reason == "end_turn"
    assert result.usage.input_tokens == 10
    assert result.usage.total_tokens == 15


def test_permission_result_has_no_allow_always_shortcut() -> None:
    """★ v1 只映射 allow_once / reject_once。

    「永远允许」是全局策略，必须过权限管理那一篇 —— 不能从一次弹窗里
    溜进来（acp 详设 §07）。这里钉住构造器没给它开后门。
    """
    helpers = {n for n in vars(RequestPermissionResult) if not n.startswith("_")}
    assert "selected" in helpers and "cancelled" in helpers
    assert not any("always" in n for n in helpers)


def test_session_update_notification_parses_its_payload() -> None:
    note = SessionUpdateNotification(
        **{
            "sessionId": "sess-1",
            "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "hi"}},
        }
    )
    assert note.session_id == "sess-1"
    assert note.parsed().content.text == "hi"


def test_bridge_package_is_a_leaf_too() -> None:
    """★ bridge 不得 import atlas_server。

    它跑在 Pod 里、随 Pod 生灭，与 server 的唯一接口是 WS 上的线协议。
    代码级共享会让「升级 server 必须同步升级全部在跑的 Pod」，而 Pod 在跑
    时没法原地升级（acp 详设 §03）。
    """
    modules = ("adapter", "ws", "lifecycle", "main")
    code = (
        "import sys, importlib;"
        f"[importlib.import_module('atlas_bridge.' + m) for m in {modules!r}];"
        f"bad=[m for m in {_FORBIDDEN!r} if m in sys.modules];"
        "print(','.join(bad))"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    leaked = proc.stdout.strip()
    assert leaked == "", f"atlas_bridge 泄漏了不该有的依赖：{leaked}"


# ──────────────────────────────────────────────── 能力位的**真实**线上形状


def test_capabilities_parse_the_shape_the_real_cli_sends() -> None:
    """★ 回归钉：claude-code-acp 实际发的就是这一份。

    两处都曾经错：字段名是 `sessionCapabilities`（不是 `session`），
    而 resume 用**键存在**表示支持（值是空对象，不是 true）。

    错了的后果不报错：can_resume 恒为 False，于是每一轮都 lost 降级、
    从头开始。而 lost 是"设计内的降级"，事件流看上去一切正常 ——
    只有对着真 CLI 连着问两轮才会发现它不记得上一轮。
    """
    from atlas_acp.caps import AgentCaps

    real = {
        "promptCapabilities": {"image": True, "embeddedContext": True},
        "mcpCapabilities": {"http": True, "sse": True},
        "loadSession": True,
        "sessionCapabilities": {"fork": {}, "list": {}, "resume": {}},
    }
    caps = AgentCaps(**real)

    assert caps.load_session is True
    assert caps.session.resume is True
    assert caps.can_resume is True


def test_a_cli_without_resume_is_not_treated_as_resumable() -> None:
    """没有 resume 键 = 不支持。恢复要两个能力位同时成立。

    「以为在继续、实际从零开始」是最坏的失败形态，所以这里宁可保守。
    """
    from atlas_acp.caps import AgentCaps

    no_resume = AgentCaps(loadSession=True, sessionCapabilities={"fork": {}, "list": {}})
    assert no_resume.can_resume is False
    assert AgentCaps(loadSession=False, sessionCapabilities={"resume": {}}).can_resume is False
    assert AgentCaps().can_resume is False
