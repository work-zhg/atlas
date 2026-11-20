"""技能状态机与发布流水线（真 Postgres 测试库 + moto 桶）。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from atlas_skill.db.models import SkillVersion
from atlas_skill.db.session import get_sessionmaker
from atlas_skill.errors import Conflict, Forbidden, Invalid, NotFound
from atlas_skill.skill.package import RawPackage
from atlas_skill.skill.service import SkillService
from atlas_skill.storage import legacy_prefix, published_prefix
from skill_testkit import TEST_TENANT, skill_files


@pytest.fixture
async def svc(storage, settings) -> AsyncIterator[SkillService]:  # type: ignore[no-untyped-def]
    async with get_sessionmaker()() as session:
        yield SkillService(session=session, storage=storage, settings=settings)
        await session.commit()


def _pkg(**kw) -> RawPackage:  # type: ignore[no-untyped-def]
    return RawPackage(files=skill_files(**kw))


async def _release(svc: SkillService, pkg: RawPackage) -> SkillVersion:
    """上传 + 另一个人审核通过（多租户以后没有免审的 builtin 上传）。"""
    row = await svc.ingest(pkg, source="tenant", actor="a")
    return await svc.review(row.id, reviewer="reviewer", approve=True, note="ok")


async def test_builtin_upload_is_refused(svc: SkillService) -> None:
    """产品自带的技能走技能市场：作者选 builtin 跳过审查的口子关掉了。"""
    with pytest.raises(Invalid, match="技能市场"):
        await svc.ingest(_pkg(), source="builtin", actor="alice")


async def test_published_package_lives_under_tenant(svc: SkillService, storage) -> None:  # type: ignore[no-untyped-def]
    row = await _release(svc, _pkg())
    assert (row.status, row.version) == ("published", 1)
    assert row.storage_key == published_prefix("demo", 1) == f"{TEST_TENANT}/_skills/demo/1/"
    assert storage.list_keys(f"{TEST_TENANT}/_skills/demo/1/") == [
        f"{TEST_TENANT}/_skills/demo/1/SKILL.md"
    ]
    assert storage.list_keys(f"{TEST_TENANT}/_skills_drafts/") == []  # 草稿已清
    assert (await svc.runtime_version("demo", 1)).storage_key == row.storage_key


async def test_scripts_require_review_by_someone_else(svc: SkillService) -> None:
    row = await svc.ingest(
        _pkg(extra={"scripts/run.py": b"print(1)"}), source="tenant", actor="alice"
    )
    assert (row.status, row.version) == ("pending_review", None)
    with pytest.raises(Forbidden):
        await svc.review(row.id, reviewer="alice", approve=True, note="自己批自己")
    approved = await svc.review(row.id, reviewer="bob", approve=True, note="看过脚本")
    assert (approved.status, approved.version, approved.reviewed_by) == ("published", 1, "bob")


async def test_tenant_skill_always_reviewed(svc: SkillService) -> None:
    row = await svc.ingest(_pkg(), source="tenant", actor="alice")
    assert row.status == "pending_review"


async def test_imported_needs_origin(svc: SkillService) -> None:
    with pytest.raises(Invalid):
        await svc.ingest(_pkg(), source="imported", actor="alice")


async def test_rejected_upload_consumes_no_version(svc: SkillService, storage) -> None:  # type: ignore[no-untyped-def]
    first = await svc.ingest(_pkg(), source="tenant", actor="alice")
    rejected = await svc.review(first.id, reviewer="bob", approve=False, note="描述不清")
    assert (rejected.status, rejected.version) == ("rejected", None)
    assert storage.list_keys(f"{TEST_TENANT}/_skills_drafts/") == []
    second = await svc.ingest(_pkg(body="改好了\n"), source="tenant", actor="alice")
    published = await svc.review(second.id, reviewer="bob", approve=True, note="ok")
    assert published.version == 1


async def test_versions_increment_and_duplicates_are_refused(svc: SkillService) -> None:
    await _release(svc, _pkg())
    v2 = await _release(svc, _pkg(body="第二版\n"))
    assert v2.version == 2
    with pytest.raises(Conflict, match="完全相同"):
        await svc.ingest(_pkg(body="第二版\n"), source="tenant", actor="a")


async def test_one_pending_upload_at_a_time(svc: SkillService) -> None:
    await svc.ingest(_pkg(), source="tenant", actor="a")
    with pytest.raises(Conflict, match="待审查"):
        await svc.ingest(_pkg(body="又一版\n"), source="tenant", actor="a")


async def test_source_cannot_change(svc: SkillService) -> None:
    await _release(svc, _pkg())
    with pytest.raises(Conflict, match="来源"):
        await svc.ingest(_pkg(body="x\n"), source="imported", actor="a", origin_url="https://x")


async def test_status_transitions(svc: SkillService) -> None:
    await _release(svc, _pkg())
    assert (
        await svc.change_status("demo", 1, action="disable", actor="a", reason=None)
    ).status == "disabled"
    assert (
        await svc.change_status("demo", 1, action="enable", actor="a", reason=None)
    ).status == "published"
    with pytest.raises(Invalid, match="原因"):
        await svc.change_status("demo", 1, action="revoke", actor="a", reason=None)
    revoked = await svc.change_status("demo", 1, action="revoke", actor="a", reason="脚本外传数据")
    assert revoked.status == "revoked"
    # ★ 下架不可撤销
    with pytest.raises(Conflict):
        await svc.change_status("demo", 1, action="enable", actor="a", reason=None)
    assert [(r.slug, r.version) for r in await svc.revoked()] == [("demo", 1)]


async def test_catalog_hides_unreleased_and_latest_is_published_only(svc: SkillService) -> None:
    await svc.ingest(_pkg(name="pending-only"), source="tenant", actor="a")
    await _release(svc, _pkg())
    await _release(svc, _pkg(body="v2\n"))
    await svc.change_status("demo", 2, action="disable", actor="a", reason=None)

    catalog = {c.slug: c for c in await svc.catalog()}
    assert "pending-only" not in catalog  # 运行时不知道它存在
    assert catalog["demo"].latest == 1
    assert [(v.version, v.status) for v in catalog["demo"].versions] == [
        (1, "published"),
        (2, "disabled"),
    ]

    with pytest.raises(NotFound):
        await svc.runtime_version("pending-only", 1)
    assert (await svc.runtime_version("demo", 2)).status == "disabled"


async def test_publish_clears_residue_from_failed_attempt(svc: SkillService, storage) -> None:  # type: ignore[no-untyped-def]
    storage.put_tree(published_prefix("demo", 1), {"stale.md": b"left over"})
    await _release(svc, _pkg())
    expected = [f"{TEST_TENANT}/_skills/demo/1/SKILL.md"]
    assert storage.list_keys(published_prefix("demo", 1)) == expected


async def test_adopt_existing_package(svc: SkillService, storage) -> None:  # type: ignore[no-untyped-def]
    """多租户之前放在桶根 _skills/ 的包：原地登记进当前租户，对象不动。"""
    storage.put_tree(legacy_prefix("legacy", 3), skill_files(name="legacy") | {"a.py": b"x"})
    row = await svc.adopt("legacy", 3, actor="ops")
    assert (row.status, row.version, row.has_scripts, row.file_count) == ("published", 3, True, 2)
    assert row.storage_key == "_skills/legacy/3/"
    with pytest.raises(Conflict):
        await svc.adopt("legacy", 3, actor="ops")
    with pytest.raises(NotFound):
        await svc.adopt("legacy", 9, actor="ops")


async def test_adopt_rejects_name_mismatch(svc: SkillService, storage) -> None:  # type: ignore[no-untyped-def]
    storage.put_tree(legacy_prefix("dir-name", 1), skill_files(name="other-name"))
    with pytest.raises(Invalid, match="不一致"):
        await svc.adopt("dir-name", 1, actor="ops")


async def test_rescan_appends_history(svc: SkillService) -> None:
    await _release(svc, _pkg())
    row = await svc.rescan("demo", 1, actor="sec")
    assert len(row.scan_result["history"]) == 1
    assert row.scan_result["history"][0]["by"] == "sec"


async def test_read_file_only_listed_paths(svc: SkillService) -> None:
    row = await _release(svc, _pkg())
    assert (await svc.read_file(row, "SKILL.md")).startswith(b"---")
    with pytest.raises(NotFound):
        await svc.read_file(row, "../../etc/passwd")


async def test_concurrent_uploads_only_one_waits_for_review(storage, settings) -> None:  # type: ignore[no-untyped-def]
    """两个上传同时进来：skill 行锁让它们排队，第二个看到已有待审查的就被拒，不会两个都挂着。"""
    async with get_sessionmaker()() as s:
        await _release(SkillService(s, storage, settings), _pkg())
        await s.commit()

    async def upload(body: str) -> str:
        async with get_sessionmaker()() as s:
            try:
                await SkillService(s, storage, settings).ingest(
                    _pkg(body=body), source="tenant", actor="a"
                )
            except Conflict:
                return "conflict"
            await s.commit()
            return "pending"

    results = await asyncio.gather(upload("x\n"), upload("y\n"))
    assert sorted(results) == ["conflict", "pending"]
