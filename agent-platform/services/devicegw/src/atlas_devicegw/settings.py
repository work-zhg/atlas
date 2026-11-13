"""设备网关的设置（环境变量前缀 ATLAS_DEVICEGW_）。"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["GatewaySettings", "get_settings"]


def _repo_root() -> Path:
    """仓库根：往上第一个有 uv.lock 的目录。装进镜像后找不到，退回 parents[3]。"""
    here = Path(__file__).resolve()
    return next((p for p in here.parents if (p / "uv.lock").is_file()), here.parents[3])


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ATLAS_DEVICEGW_",
        env_file=_repo_root() / ".env",
        extra="ignore",
    )

    #: 在线表、会话登记（多个实例共享）
    redis_url: str = "redis://localhost:6380/0"
    #: Agent 服务的内部接口：核对配对码、设备令牌
    agent_url: str = "http://127.0.0.1:8000"
    #: 设备看到的网关地址（公网入口 + /device-api）：能力代理的地址 = 它 + /u/{上游编号}
    public_url: str = "http://127.0.0.1:8095"
    #: 本实例的内部地址：写进在线表，Agent 服务按它直接连设备所在的实例。
    #: 空 = 按 POD_IP（K8s 注入）+ 端口拼
    instance_url: str = ""
    #: Agent 服务连会话流时带的令牌（内网也校验一道）；空 = 不校验
    internal_token: str = ""

    #: 连接器的最低版本：低于它的拒绝并提示升级
    min_connector_version: str = "1.0.0"
    #: 在线表条目的过期时间；心跳每 1/3 续一次。实例崩溃时条目自己消失
    presence_ttl_s: int = 90
    #: 会话登记的过期时间；每次登记、每次代理调用都续
    binding_ttl_s: int = 7 * 24 * 3600
    #: 一路的窗口迟迟不还，超过这么久就只关这一路
    stall_s: float = 30.0
    #: 会话流里单条消息的上限（与 bridge 一致）
    max_message_bytes: int = 32 * 1024 * 1024
    #: 能力代理建连接的超时。★ 不设读超时：模型、MCP 调用可能要很久
    proxy_connect_timeout_s: float = 10.0

    http_port: int = 8095

    def own_url(self) -> str:
        if self.instance_url:
            return self.instance_url.rstrip("/")
        ip = os.environ.get("POD_IP", "127.0.0.1")
        return f"http://{ip}:{self.http_port}"


@lru_cache
def get_settings() -> GatewaySettings:
    return GatewaySettings()
