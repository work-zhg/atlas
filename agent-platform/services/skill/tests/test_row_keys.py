"""表按约定改成自增 id + uuid（迁移 c0006）；待审查用部分唯一索引兜底。"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from atlas_skill.check.package import RawPackage
from atlas_skill.db.models import SkillVersion
from atlas_skill.db.session import get_sessionmaker
from atlas_skill.versions.service import SkillService
from conftest import TEST_URL
from skill_testkit import TEST_TENANT, skill_files
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine


async def test_one_pending_upload_per_skill_is_enforced_by_the_database(storage, settings) -> None:  # type: ignore[no-untyped-def]
    """服务先查再插；两个并发上传都查到「没有待审查的」时，部分唯一索引挡掉后到的那个。"""
    async with get_sessionmaker()() as session:
        svc = SkillService(session=session, storage=storage, settings=settings)
        row = await svc.ingest(RawPackage(files=skill_files()), source="tenant", actor="alice")
        twin = {c: getattr(row, c) for c in ("description", "frontmatter", "content_hash")}
        session.add(
            SkillVersion(
                skill_uuid=row.skill_uuid,
                status="pending_review",
                storage_key="x/",
                size_bytes=0,
                file_count=0,
                has_scripts=False,
                files=[],
                scan_result={},
                uploaded_by="bob",
                **twin,
            )
        )
        with pytest.raises(IntegrityError, match="uq_skill_version_pending"):
            await session.flush()


async def test_c0006_backfills_row_keys() -> None:
    """c0005 → c0006：上传 id 原值保留成 uuid，版本按 (租户, slug) 接上技能的 uuid，
    审计的 at 成 created_at。"""
    from alembic import command
    from alembic.config import Config
    from atlas_skill.__main__ import MIGRATIONS_DIR
    from atlas_skill.settings import get_settings

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    tenant, upload, audit_id = UUID(TEST_TENANT), uuid4(), uuid4()
    await asyncio.to_thread(command.downgrade, cfg, "c0005_tenant_market")
    engine = create_async_engine(TEST_URL)
    try:
        async with engine.begin() as conn:
            for table in ("skill_audit", "skill_version", "skill", "market_skill_version"):
                await conn.execute(text(f"DELETE FROM {table}"))
            await conn.execute(text("DELETE FROM market_skill"))
            await conn.execute(
                text(
                    "INSERT INTO skill (tenant_uuid, slug, source, created_by, created_at)"
                    " VALUES (:t, 'demo', 'tenant', 'a', now())"
                ),
                {"t": tenant},
            )
            await conn.execute(
                text(
                    "INSERT INTO skill_version (id, tenant_uuid, slug, version, status,"
                    " description, frontmatter, storage_key, content_hash, size_bytes,"
                    " file_count, has_scripts,"
                    " files, scan_result, uploaded_by, created_at, updated_at) VALUES (:u, :t,"
                    " 'demo', 1, 'published', 'd', '{}', 'k/', 'h', 1, 1, false, '[]', '{}', 'a',"
                    " now(), now())"
                ),
                {"u": upload, "t": tenant},
            )
            await conn.execute(
                text(
                    "INSERT INTO skill_audit (id, tenant_uuid, at, actor, action, target_kind,"
                    " target_id, detail) VALUES (:u, :t, now(), 'a', 'skill.upload', 'skill',"
                    " 'demo', '{}')"
                ),
                {"u": audit_id, "t": tenant},
            )
            await conn.execute(
                text(
                    "INSERT INTO market_skill (slug, install_by_default, created_by, created_at)"
                    " VALUES ('browser', true, 'ops', now())"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO market_skill_version (id, slug, version, status, description,"
                    " frontmatter, storage_key, content_hash, size_bytes, file_count, has_scripts,"
                    " files, scan_result, published_by, published_at, updated_at) VALUES (:u,"
                    " 'browser', 3, 'published', 'd', '{}', 'm/', 'h', 1, 1, false, '[]', '{}',"
                    " 'ops', now(), now())"
                ),
                {"u": uuid4()},
            )
    finally:
        await engine.dispose()
        await asyncio.to_thread(command.upgrade, cfg, "head")

    engine = create_async_engine(TEST_URL)
    try:
        async with engine.connect() as conn:
            skill: Any = (await conn.execute(text("SELECT id, uuid FROM skill"))).one()
            version: Any = (
                await conn.execute(text("SELECT uuid, skill_uuid, version FROM skill_version"))
            ).one()
            audit: Any = (
                await conn.execute(text("SELECT uuid, created_at FROM skill_audit"))
            ).one()
            market: Any = (
                await conn.execute(
                    text(
                        "SELECT s.slug, v.version FROM market_skill_version v"
                        " JOIN market_skill s ON s.uuid = v.market_skill_uuid"
                    )
                )
            ).one()
    finally:
        await engine.dispose()
    assert skill.id and skill.uuid
    assert (version.uuid, version.skill_uuid, version.version) == (upload, skill.uuid, 1)
    assert audit.uuid == audit_id and audit.created_at is not None
    assert tuple(market) == ("browser", 3)
