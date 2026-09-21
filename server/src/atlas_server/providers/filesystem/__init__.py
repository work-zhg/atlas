"""会话工作区的实现。"""

from atlas_server.providers.filesystem.oss import OssFilesystem, PathEscape

__all__ = ["OssFilesystem", "PathEscape", "make_workspace"]


def make_workspace(settings, user_id, thread_id, workspace_thread_id=None):  # noqa: ANN001, ANN201
    """按配置构造会话工作区；没配对象存储则返回 None。

    workspace_thread_id: 工作区归属的会话。None = 自己；子智能体传父会话的 id
        —— 它与主 agent 共享同一份工作区，而 skills / system 仍是自己的。

    ★ 返回 None 不是降级，是**关闭文件能力**：build_agent 会让文件工具
      一个都不注册、大结果外置一并关闭。回落到某个内存实现才是有害的 ——
      模型以为自己有持久工作区，写进去的东西 run 结束即弃却毫无提示。

    client 每次新建：boto3 的 client 是线程安全的，但它持有连接池，
    而会话工作区的生命周期由 run 决定 —— 让它跟着 run 走比缓存一个
    进程级单例更简单，且对象存储调用的建连开销相对模型调用可忽略。
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
    return OssFilesystem(
        client,
        bucket=settings.oss_bucket,
        user_id=user_id,
        thread_id=thread_id,
        workspace_thread_id=workspace_thread_id,
    )
