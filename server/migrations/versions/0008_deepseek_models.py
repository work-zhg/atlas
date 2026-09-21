"""model_catalog 收录 DeepSeek 的两个模型

Revision ID: 0008_deepseek_models
Revises: 0007_subagent_sessions
Create Date: 2026-09-20

## 为什么收进产品的目录，而不是让每个部署自己往库里插

`model_catalog` 存的是**能力元数据**（上下文窗口、支不支持 thinking / cache /
effort），这些是模型本身的属性，跟部署无关 —— 同一个模型在谁那儿都是这些
能力。真正随部署变的是「这个网关能不能服务它」，那是 `is_available`，由
网关巡检刷新（services/meta.py）。

所以新增模型属于产品知识，进迁移；可用性属于部署状态，不进。

## 能力位全部来自实测，不是抄文档

对着 https://api.deepseek.com/anthropic 逐项打过：

    thinking      ✓ 返回 thinking 内容块
    tool_use      ✓ 返回 tool_use 块（native 的文件工具靠它）
    temperature   ✓ 接受
    cache         ✗ cache_control 被接受，但 cache_creation_input_tokens 恒为 0
    effort        ✗ 参数被接受（200），但无法证明它改变了行为

★ cache 标 False 不等于"没有缓存收益" —— 实测 usage 里 cache_read 有值，
  DeepSeek 自己在做上下文缓存。这个开关控制的是"要不要在请求里塞
  cache_control 块"，而塞了不起作用，所以关掉。

★ effort / adaptive_thinking 这类"接受但无法证明生效"的，一律取 False：
  标 True 会让编辑器给出一个可能毫无作用的旋钮，而用户没法分辨它有没有用。

★ context_window 取保守值 64K。估高了会在对话中途硬失败（不可恢复），
  估低了只是压缩触发得早一点（可恢复）—— 两种错的代价不对称。

## 这个端点会静默别名模型名

传 `claude-sonnet-5` 它照样 200，实际跑的是 `deepseek-flash`。它自报的
合法名字只有 `deepseek-flash` 与 `deepseek-v4-pro`（来自它对未知模型名的
报错）。所以指向这个网关的部署应当把 claude-* 标成不可用 —— 否则界面上
写着 Claude、真正答话的是 DeepSeek。那是部署侧的动作，不在本迁移里。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_deepseek_models"
down_revision = "0007_subagent_sessions"
branch_labels = None
depends_on = None

_MODELS: tuple[dict[str, object], ...] = (
    {
        "model": "deepseek-flash",
        # ★ provider 是**线协议方言**（anthropic / openai），不是厂商。
        #   DeepSeek 这个端点说的就是 Anthropic 协议。
        "provider": "anthropic",
        "display_name": "DeepSeek Flash",
        "context_window": 64_000,
        "max_output_tokens": 8_192,
        "supports_thinking": True,
        "supports_adaptive_thinking": False,
        "supports_effort": False,
        "supports_cache": False,
        "supports_temperature": True,
        "min_cacheable_tokens": 0,
    },
    {
        "model": "deepseek-v4-pro",
        "provider": "anthropic",
        "display_name": "DeepSeek V4 Pro",
        "context_window": 64_000,
        "max_output_tokens": 8_192,
        "supports_thinking": True,
        "supports_adaptive_thinking": False,
        "supports_effort": False,
        "supports_cache": False,
        "supports_temperature": True,
        "min_cacheable_tokens": 0,
    },
)


def upgrade() -> None:
    catalog = sa.table(
        "model_catalog",
        sa.column("model", sa.String),
        sa.column("provider", sa.String),
        sa.column("display_name", sa.String),
        sa.column("context_window", sa.Integer),
        sa.column("max_output_tokens", sa.Integer),
        sa.column("supports_thinking", sa.Boolean),
        sa.column("supports_adaptive_thinking", sa.Boolean),
        sa.column("supports_effort", sa.Boolean),
        sa.column("supports_cache", sa.Boolean),
        sa.column("supports_temperature", sa.Boolean),
        sa.column("min_cacheable_tokens", sa.Integer),
    )
    # ★ 先删后插：本迁移在某些开发库上是"补插"（那两行已被手工加过）。
    #   直接 insert 会撞主键，而 bulk_insert 没有 upsert 语义。
    op.execute(
        sa.text("DELETE FROM model_catalog WHERE model IN ('deepseek-flash', 'deepseek-v4-pro')")
    )
    op.bulk_insert(catalog, list(_MODELS))


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM model_catalog WHERE model IN ('deepseek-flash', 'deepseek-v4-pro')")
    )
