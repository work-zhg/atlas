"""AgentCaps —— adapter 在 initialize 里声明的能力位。

★ 能力是**探测出来的，不是假设的**。Claude Code 的 adapter 可能声明
  loadSession，Codex 的可能不声明 —— 同为 acp 类型，一个能无损恢复会话，
  另一个每次冷启动就丢上下文（Agent 管理 §07 / acp 详设 §05）。

配置平面按 cli_type 的经验表推导出 caps_declared（编辑器展示用）；
首次连接拿到的本对象落库成 caps_probed。两者不一致要发 caps.mismatch
显式事件 —— 静默按实测收窄能力，用户会看到一个「昨天还能继续、今天
每次都从头」的无法解释的行为变化。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["AgentCaps", "FsCaps", "SessionCaps"]


class FsCaps(BaseModel):
    """adapter 反向请求宿主读写文件的能力。

    ★ 默认全关。每打开一个都要想清楚它在 Pod 里意味着什么权限
      （执行环境 §06）—— 打开 writeTextFile 等于允许 CLI 绕过审批回路
      直接改宿主文件。Atlas 的 CLI 跑在自己的 Pod 里、/workspace 就是
      会话前缀，没有理由再开这条反向通道。
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    read_text_file: bool = Field(default=False, alias="readTextFile")
    write_text_file: bool = Field(default=False, alias="writeTextFile")


class SessionCaps(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    #: 能否恢复一个已存在的会话。与 AgentCaps.load_session 一起决定
    #: §10 的恢复路径走 session/load 还是降级为 lost + session/new。
    resume: bool = False

    @field_validator("resume", mode="before")
    @classmethod
    def _presence_means_supported(cls, v: Any) -> Any:
        """★ 真 adapter 用「键存在」表示支持，值是个空对象而不是 true。

        claude-code-acp 实测返回：
            "sessionCapabilities": {"fork": {}, "list": {}, "resume": {}}

        按 bool 解析的话 `{}` 根本过不了校验，于是整个 sessionCaps 退回默认值、
        can_resume 恒为 False —— 表现是**每一轮都从头开始**，而且因为
        lost 降级是"设计内的"，事件流看上去一切正常。
        """
        if isinstance(v, dict):
            return True
        if v is None:
            return False
        return v


class AgentCaps(BaseModel):
    """initialize 的响应体。

    ★ 字段全部有默认值且保守（False）：adapter 没声明的能力一律当作没有。
      反过来（默认 True）的后果是调用一个不存在的方法，错误发生在
      运行中而不是握手时。
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    #: adapter 是否实现 session/load。
    load_session: bool = Field(default=False, alias="loadSession")
    #: ★ 线上的字段名是 sessionCapabilities。此前这里只认 `session`，
    #:   于是真 adapter 声明的 resume 一个字都没被读到。
    #:   populate_by_name=True，所以两个名字都收。
    session: SessionCaps = Field(default_factory=SessionCaps, alias="sessionCapabilities")
    fs: FsCaps = Field(default_factory=FsCaps)
    #: adapter 是否提供终端能力（Atlas 不用 —— execute 归平台的沙箱协议）
    terminal: bool = False

    @property
    def can_resume(self) -> bool:
        """恢复需要**两个**能力位同时成立。

        ★ 只看 loadSession 不够：有的 adapter 实现了方法但会话本身不可恢复
          （sessionCapabilities.resume=false）。缺任一个都走 lost 降级 ——
          「以为在继续、实际从零开始」是最坏的失败形态（Subagent §04）。
        """
        return self.load_session and self.session.resume
