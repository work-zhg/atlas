"""会话工作区（c4-model/agent/code-workspace.html）。

    paths      路径关卡：先规范化、再看落没落在本会话能用的根里（所用工作区、绑定的技能版本）
    stores     两种底层：直连对象存储、经沙箱
    router     每次操作选底层：这个人的原生沙箱 READY 时经沙箱，否则直连
    workspace  一个 run 的文件能力（实现引擎的 FilesystemProtocol）
    listing    界面上的工作区：列、打开、预览、上传

布局在 domain/workspace_layout.py（和沙箱共用的共享内核）。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from .listing import PreviewObject, WorkspaceListing
from .paths import FilePath, PathEscape, PathGate
from .router import WorkspaceRouting
from .workspace import Workspace

__all__ = [
    "FilePath",
    "PathEscape",
    "PathGate",
    "PreviewObject",
    "Workspace",
    "WorkspaceListing",
    "WorkspaceRouting",
    "make_workspace",
]


def make_workspace(
    settings: Any,
    user_id: UUID | str,
    thread_id: UUID | str,
    workspace_thread_id: UUID | str | None = None,
    *,
    skills: Iterable[tuple[str, int | str]] = (),
    routing: WorkspaceRouting | None = None,
) -> Workspace | None:
    """按配置构造会话工作区；没配对象存储则返回 None。

    workspace_thread_id: 所用工作区的根会话。None = 自己；子、孙会话传根会话的 id
        —— 它们共用同一份工作区，隐藏目录 .atlas/{会话}/ 仍按自己。
    skills: 本智能体绑定的技能 (slug, 版本)：文件工具只放行这几个技能目录。
    routing: 沙箱路由。这个人的沙箱在跑时，读写经它走 Pod 里的挂载；
        None = 永远直连对象存储。

    ★ 返回 None 不是降级，是**关闭文件能力**：文件工具一个都不注册。回落到某个内存实现才是有害的 ——
      模型以为自己有持久工作区，写进去的东西 run 结束即弃却毫无提示。

    client 每次新建：boto3 的 client 是线程安全的，但它持有连接池，而会话工作区的生命周期由 run 决定
    —— 让它跟着 run 走比缓存一个进程级单例更简单，且对象存储调用的建连开销相对模型调用可忽略。
    """
    if not settings.workspace_configured:
        return None

    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=settings.oss_endpoint,
        region_name=settings.oss_region,
        aws_access_key_id=(
            settings.oss_access_key_id.get_secret_value() if settings.oss_access_key_id else None
        ),
        aws_secret_access_key=(
            settings.oss_access_key_secret.get_secret_value()
            if settings.oss_access_key_secret
            else None
        ),
        config=Config(
            s3={"addressing_style": settings.oss_addressing_style},
            # ★ 必须关掉 boto3 的默认上传校验和。
            #
            #   botocore >= 1.36 起，PutObject 默认带上 x-amz-sdk-checksum-algorithm
            #   与 x-amz-checksum-crc32。阿里云 OSS 与 GCS 的 S3 兼容层都不认这套
            #   新流程，签名算出来对不上 —— 报的还是 **SignatureDoesNotMatch**，
            #   指向凭据而不是指向真因，排查会先去翻 key。
            #
            #   moto 不校验签名，所以这个 bug 在内存测试里永远看不见：
            #   第一次对着真 GCS 写文件就撞上了。
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )
    return Workspace(
        client,
        bucket=settings.oss_bucket,
        user_id=user_id,
        thread_id=thread_id,
        workspace_thread_id=workspace_thread_id,
        skills=skills,
        routing=routing,
    )
