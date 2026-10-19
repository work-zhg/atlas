"""run.status 加 'suspended'：一轮可以分多段执行

Revision ID: 0010_run_suspended
Revises: 0009_message_kind
Create Date: 2026-09-23

一次委派可能要跑一小时。父 run 在进程里等那么久的代价是明确的：部署一次
就全丢，而丢掉的是子 run 已经干完的活。于是父 run 在委派的边界上停下来，
把状态交给数据库，子 run 跑完后再续跑。

`suspended` 就是那个停下来的地方。它与已有的六个状态有一条本质区别 ——
**它是合法的静止态，不是终态**：

    queued / running / awaiting_approval   有进程在跑（或马上要跑）
    suspended                              没有进程，但这个 run 没结束
    succeeded / failed / cancelled / interrupted   结束了，事件集不可变

这条区别决定了它在两处**必须**被区别对待：

  · `reap_orphans` **不能**收它。那个扫描的前提是「标着在跑却没有进程 =
    孤儿」，而 suspended 正是「没有进程」的正常形态。收了的话每次部署都会
    把所有等待中的委派判死，而它们的子 run 还在好好地跑。

  · `active_run_of` **必须**认它。那是「刷新页面后接回事件流」的查询 ——
    用户眼里这个 run 还在跑（他正等着子智能体的结果），查不到就白屏。

索引的 WHERE 因此要跟着改，否则 active_run_of 查 suspended 走不了索引。
★ 分部索引的 WHERE 是**查询优化不是约束**（迁移 0001 的原话），MySQL 分支
  本来就没有 WHERE，不用动。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_run_suspended"
down_revision: str | Sequence[str] | None = "0009_message_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_STATUSES = "'queued','running','succeeded','failed','cancelled','interrupted','awaiting_approval'"
_NEW_STATUSES = f"{_OLD_STATUSES},'suspended'"

_OLD_ACTIVE = "'queued','running','awaiting_approval'"
_NEW_ACTIVE = f"{_OLD_ACTIVE},'suspended'"

#: ★ 约束的**真名**，不是 models.py 里那个 `name="status_enum"`。
#:
#:   naming_convention 会给它加一次 `ck_<table>_` 前缀，而 0001 建表时传进去
#:   的名字本身已经带着一层 —— 实际落在库里的是双前缀的这一串。
#:   `op.drop_constraint("status_enum", ...)` 会去找 `ck_run_status_enum`
#:   然后报「约束不存在」（踩过一次）。写死真名，不让 convention 再插手。
_CK = "ck_run_ck_run_status_enum"


def _dialect() -> str:
    return op.get_bind().dialect.name


def _swap_check(statuses: str) -> None:
    op.execute(f"ALTER TABLE run DROP CONSTRAINT {_CK}")
    op.execute(f"ALTER TABLE run ADD CONSTRAINT {_CK} CHECK (status IN ({statuses}))")


def _swap_index(statuses: str) -> None:
    if _dialect() in ("mysql", "mariadb"):
        return  # MySQL 无分部索引，那边的 ix_run_active 本来就没有 WHERE
    op.execute("DROP INDEX ix_run_active")
    op.execute(f"CREATE INDEX ix_run_active ON run (status) WHERE status IN ({statuses})")


def upgrade() -> None:
    _swap_check(_NEW_STATUSES)
    _swap_index(_NEW_ACTIVE)


def downgrade() -> None:
    # 回滚前必须先把 suspended 的 run 归位，否则新的 CHECK 建不上 ——
    # 它们是「没跑完」的 run，按中断处理与 reap_orphans 的口径一致。
    op.execute(
        "UPDATE run SET status = 'interrupted', error_kind = 'interrupted', "
        "error_message = '回滚 0010 时中断' WHERE status = 'suspended'"
    )
    _swap_check(_OLD_STATUSES)
    _swap_index(_OLD_ACTIVE)
