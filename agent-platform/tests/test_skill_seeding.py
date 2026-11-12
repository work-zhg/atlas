"""技能拷进这个人的技能目录 {user}/skills/{slug}/{版本}/（沙箱里是 /data/skills/…）。

三个层级各验一遍 —— L3（含可执行脚本）是这条路径的关键：技能在 /data 下，沙箱挂的就是同一份
数据，脚本在容器里天然有真实路径。一个人一份、缺了才拷、拷完写完成标记。
"""

from __future__ import annotations

from uuid import uuid4

import boto3
import pytest
from atlas_agent.providers.filesystem import Workspace
from atlas_agent.providers.filesystem.skill_copy import (
    COMPLETE_MARKER,
    SkillMeta,
    SkillUnavailable,
    seed_session_skills,
)
from atlas_engine.kernel.middleware.skills import SkillRef, SkillsMiddleware
from moto import mock_aws

BUCKET, USER = "atlas-test", "u-test"


@pytest.fixture
def env():
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        yield s3


def _ws(s3, *skills: tuple[str, int], user: str = USER) -> Workspace:
    """一个新会话的工作区，绑了这些技能版本。"""
    return Workspace(
        s3, bucket=BUCKET, user_id=user, thread_id=uuid4(), workspace_dir="w", skills=skills
    )


def _publish(s3, slug: str, version: int, files: dict[str, str]) -> None:
    """往技能区发布一个版本。"""
    for rel, body in files.items():
        s3.put_object(Bucket=BUCKET, Key=f"_skills/{slug}/{version}/{rel}", Body=body.encode())


async def test_l1_instructions_only(env) -> None:
    s3 = env
    fs = _ws(s3, ("style", 1))
    _publish(s3, "style", 1, {"SKILL.md": "# 写作口径"})

    refs = await seed_session_skills(fs, [SkillMeta("style", 1, "写作口径")])

    path = "/data/skills/style/1/SKILL.md"
    assert refs == [SkillRef(name="style", description="写作口径", path=path)]
    assert fs.read(path).file_data["content"] == "# 写作口径"


async def test_l2_with_references(env) -> None:
    s3 = env
    fs = _ws(s3, ("dataviz", 2))
    _publish(s3, "dataviz", 2, {"SKILL.md": "# 图表", "references/color.md": "配色"})

    await seed_session_skills(fs, [SkillMeta("dataviz", 2, "图表设计")])

    content = fs.read("/data/skills/dataviz/2/references/color.md").file_data["content"]
    assert content == "配色"


async def test_l3_scripts_land_at_a_real_path(env) -> None:
    """★ 可执行技能：脚本落在 /data 下，沙箱挂的是同一份数据，不需要第二条投送路径。"""
    s3 = env
    fs = _ws(s3, ("ui", 3))
    _publish(
        s3, "ui", 3, {"SKILL.md": "# UI", "scripts/search.py": "print(1)", "data/x.csv": "a,b"}
    )

    await seed_session_skills(fs, [SkillMeta("ui", 3, "UI 检索")])

    assert fs.read("/data/skills/ui/3/scripts/search.py").file_data["content"] == "print(1)"
    assert fs.read("/data/skills/ui/3/data/x.csv").file_data["content"] == "a,b"


async def test_missing_version_fails_loudly(env) -> None:
    """少拷几个文件就默默开始的话，表现是模型「照技能说的做了但脚本不存在」。"""
    fs = _ws(env)
    with pytest.raises(SkillUnavailable, match="v9"):
        await seed_session_skills(fs, [SkillMeta("nope", 9, "x")])


async def test_versions_sit_side_by_side(env) -> None:
    """spec 钉死版本：绑不同版本的智能体各用各的，互不覆盖。"""
    s3 = env
    _publish(s3, "style", 1, {"SKILL.md": "旧版"})
    _publish(s3, "style", 2, {"SKILL.md": "新版"})
    old, new = _ws(s3, ("style", 1)), _ws(s3, ("style", 2))

    await seed_session_skills(old, [SkillMeta("style", 1, "口径")])
    await seed_session_skills(new, [SkillMeta("style", 2, "口径")])

    assert old.read("/data/skills/style/1/SKILL.md").file_data["content"] == "旧版"
    assert new.read("/data/skills/style/2/SKILL.md").file_data["content"] == "新版"
    # 文件工具只放行本智能体绑定的版本
    assert old.read("/data/skills/style/2/SKILL.md").error


async def test_copied_once_per_user_and_shared_across_sessions(env) -> None:
    """★ 一个人一份：第二个会话不再拷；会话里改了，这个人别的会话也看得到（用户 11-09 定的）。

    技能区那份不动；别的用户有自己的一份。
    """
    s3 = env
    _publish(s3, "style", 1, {"SKILL.md": "原文"})
    a, b = _ws(s3, ("style", 1)), _ws(s3, ("style", 1))
    await seed_session_skills(a, [SkillMeta("style", 1, "口径")])
    marker = f"{USER}/skills/style/1/{COMPLETE_MARKER}"
    assert s3.head_object(Bucket=BUCKET, Key=marker)

    a.write("/data/skills/style/1/SKILL.md", "会话 a 改过")
    await seed_session_skills(b, [SkillMeta("style", 1, "口径")])  # 标记在：不重拷，不覆盖

    assert b.read("/data/skills/style/1/SKILL.md").file_data["content"] == "会话 a 改过"
    src = s3.get_object(Bucket=BUCKET, Key="_skills/style/1/SKILL.md")["Body"].read()
    assert src.decode() == "原文"
    other = _ws(s3, ("style", 1), user="u-other")
    await seed_session_skills(other, [SkillMeta("style", 1, "口径")])
    assert other.read("/data/skills/style/1/SKILL.md").file_data["content"] == "原文"


async def test_half_copied_version_is_copied_again(env) -> None:
    """没有完成标记的目录当作没拷完：崩在中途的，下次重拷。"""
    s3 = env
    _publish(s3, "ui", 1, {"SKILL.md": "x", "scripts/a.py": "1"})
    s3.put_object(Bucket=BUCKET, Key=f"{USER}/skills/ui/1/SKILL.md", Body=b"x")  # 只拷了一半
    fs = _ws(s3, ("ui", 1))

    await seed_session_skills(fs, [SkillMeta("ui", 1, "UI")])

    assert fs.read("/data/skills/ui/1/scripts/a.py").file_data["content"] == "1"


async def test_skills_do_not_pollute_workspace_listing(env) -> None:
    """模型 search("") 看到的是自己的产物，不是几十个技能文件。"""
    s3 = env
    fs = _ws(s3, ("ui", 1))
    _publish(s3, "ui", 1, {"SKILL.md": "x", "scripts/a.py": "1", "data/b.csv": "2"})
    await seed_session_skills(fs, [SkillMeta("ui", 1, "UI")])
    fs.write("report.md", "我的产物")

    result = fs.search("")
    assert result.keys == ["report.md"]
    assert result.common_prefixes == []


def test_empty_skills_list_means_no_middleware() -> None:
    """没配技能就不装中间件 —— 空技能段白占提示词。"""
    middleware = SkillsMiddleware(skills=[])
    assert middleware._skills == []


# ──────────────────────────────────────────────── 接线：谁在什么时候投送


async def test_creating_a_thread_seeds_the_agents_skills(clean_db) -> None:
    """★ 会话创建就把 agent 的技能投进去 —— 不是等首次 run。

    这条曾经是空的：主 agent 的技能根本没被投送，而 RunHooks 里的
    SkillRef 清单照常渲染给模型 —— 于是模型看得见技能的名字与描述，
    一去 read 那个路径就是 404。子智能体那边反而投了，两边不一致。
    """
    import boto3
    from atlas_agent.config import Settings
    from atlas_agent.db.session import get_sessionmaker
    from atlas_agent.repositories.agent import AgentRepository
    from atlas_agent.schemas.thread import ThreadCreate
    from atlas_agent.services.thread import ThreadService
    from moto import mock_aws

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="atlas-seed")
        s3.put_object(
            Bucket="atlas-seed", Key="_skills/dataviz/1/SKILL.md", Body="画图技能".encode()
        )
        s3.put_object(
            Bucket="atlas-seed", Key="_skills/dataviz/1/scripts/plot.py", Body=b"print(1)"
        )

        settings = Settings(litellm_key="x", oss_bucket="atlas-seed")

        async with get_sessionmaker()() as session:
            agents = AgentRepository(session)
            user_id = await _any_user_id(session)
            agent, _version = await agents.create(
                slug="skilled",
                name="带技能的",
                description="",
                avatar_key="general",
                created_by=user_id,
                spec={
                    "system_prompt": "p",
                    "model": {"model": "claude-sonnet-5"},
                    "skills": [{"slug": "dataviz", "version": 1}],
                },
            )
            await session.commit()
            agent_id = agent.agent_uuid

        async with get_sessionmaker()() as session:
            service = ThreadService(session, settings)
            out = await service.create(ThreadCreate(agent_id=agent_id), user_id=user_id)
            await session.commit()

        keys = {o["Key"] for o in s3.list_objects_v2(Bucket="atlas-seed")["Contents"]}
        # 技能落在这个人的技能目录下（按版本），不在会话目录里
        assert f"{user_id}/skills/dataviz/1/SKILL.md" in keys
        assert f"{user_id}/skills/dataviz/1/scripts/plot.py" in keys
        assert not any(k.startswith(f"{user_id}/{out.id}/") for k in keys)


async def test_thread_creation_without_object_storage_is_not_an_error(clean_db) -> None:
    """没配对象存储时不投送、也不报错。

    「没配 bucket = 没有文件能力」是个完整的产品决定：模型那边文件工具
    一个都不注册，技能自然也无处可放。这不是降级路径，不该拦住建会话。
    """
    from atlas_agent.config import Settings
    from atlas_agent.db.session import get_sessionmaker
    from atlas_agent.repositories.agent import AgentRepository
    from atlas_agent.schemas.thread import ThreadCreate
    from atlas_agent.services.thread import ThreadService

    settings = Settings(litellm_key="x")
    assert settings.workspace_configured is False

    async with get_sessionmaker()() as session:
        user_id = await _any_user_id(session)
        agent, _version = await AgentRepository(session).create(
            slug="skilled-nofs",
            name="带技能但没存储",
            description="",
            avatar_key="general",
            created_by=user_id,
            spec={
                "system_prompt": "p",
                "model": {"model": "claude-sonnet-5"},
                "skills": [{"slug": "dataviz", "version": 1}],
            },
        )
        await session.commit()
        agent_id = agent.agent_uuid

    async with get_sessionmaker()() as session:
        out = await ThreadService(session, settings).create(
            ThreadCreate(agent_id=agent_id), user_id=user_id
        )
        await session.commit()
    assert out.id is not None


async def _any_user_id(session):
    from atlas_agent.db.models import AppUser
    from sqlalchemy import select

    return (await session.execute(select(AppUser.user_uuid).limit(1))).scalars().one()
