"""Workspace 的行为契约。

用 moto 的内存 S3 —— 与真实 OSS / MinIO 是同一套 S3 API，所以这些断言
在三种环境上等价成立。

最重要的一组是 `test_path_escapes_are_rejected`：会话隔离全靠
`_to_key()`，它错了等于会话之间可以互相读写，而从调用方看毫无异常。
"""

from __future__ import annotations

from uuid import uuid4

import boto3
import pytest
from atlas_agent.providers.filesystem import PathEscape, Workspace
from atlas_agent.providers.filesystem.workspace import READ_MAX_BYTES
from atlas_engine.contracts import EDIT_MAX_BYTES, FilesystemProtocol
from moto import mock_aws

BUCKET = "atlas-test"
USER = "u-test"


@pytest.fixture
def s3():
    """一个空桶。需要自己控制会话形态的用例用它。"""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
def fs(s3):
    return Workspace(
        s3, bucket=BUCKET, user_id=USER, thread_id=uuid4(), workspace_dir="2026-11-12/t"
    )


@pytest.fixture
def two_sessions():
    """同一个桶里的两个会话 —— 用来验证隔离不是靠约定。"""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        a = Workspace(s3, bucket=BUCKET, user_id=USER, thread_id="t-a", workspace_dir="t-a")
        b = Workspace(s3, bucket=BUCKET, user_id=USER, thread_id="t-b", workspace_dir="t-b")
        yield a, b


# ──────────────────────────────────────────────── 协议一致性


def test_satisfies_filesystem_protocol(fs) -> None:
    assert isinstance(fs, FilesystemProtocol)


# ──────────────────────────────────────────────── 路径安全（最关键的一组）


@pytest.mark.parametrize(
    "escape",
    [
        "../../other/x",
        "../../../etc/passwd",
        "a/../../../../x",
        "./../../x",
        "/../../x",
    ],
)
def test_path_escapes_are_rejected(fs, escape: str) -> None:
    """越出区根的路径直接拒绝（PathGate），文件工具拿到的是错误结果。

    ★ 规范化必须在拼前缀之前做。先拼后归一的话这些路径会落到别的会话（甚至别的用户）上，
      而调用方拿到的是一个成功的 WriteResult。
    ★ 原先是把它们悄悄归进本会话的工作区：模型以为写的是 ../x，实际落在别处，收不到任何提示。
    """
    with pytest.raises(PathEscape, match="越出"):
        fs.key_of(escape)
    assert "越出" in (fs.write(escape, "x").error or "")
    assert "越出" in (fs.read(escape).error or "")


def test_dot_dot_inside_the_workspace_is_fine(fs) -> None:
    """没越界的 .. 照常规范化。"""
    assert fs.key_of("a/b/../c.md") == fs.key_of("a/c.md")
    assert fs.key_of(f"{fs.workdir}/a/../c.md") == fs.key_of("c.md")


async def test_escaping_into_another_session_is_rejected(two_sessions) -> None:
    """★ 用 ../../ 逃到同一个用户的别的会话：被拒，读不到、也写不进去（同步、异步两条路）。"""
    a, b = two_sessions
    b.write("secret.md", "b 的秘密")
    other = "../../t-b/workspace/secret.md"
    assert (await a.aread(other)).error and "越出" in (await a.aread(other)).error
    assert "越出" in ((await a.awrite(other, "覆盖")).error or "")
    assert b.read("secret.md").file_data["content"] == "b 的秘密"


@pytest.mark.parametrize("bad", ["", "/", ".", "./", "/.."])
def test_root_itself_is_not_a_file(fs, bad: str) -> None:
    """工作区根不是文件 —— 读写它必须显式失败而不是落到某个空 key 上。"""
    with pytest.raises(PathEscape):
        fs.key_of(bad)


@pytest.mark.parametrize("workdir", [True, False])
def test_root_operations_return_errors_not_raise(fs, workdir: bool) -> None:
    """模型对工作区根调用文件工具：返回错误结果让它自己纠正，而不是抛出去打断 run。"""
    bad = fs.workdir if workdir else "/"
    assert "目录本身" in (fs.read(bad).error or "")
    assert "目录本身" in (fs.write(bad, "x").error or "")
    assert "目录本身" in (fs.edit(bad, "a", "b").error or "")
    assert "目录本身" in (fs.delete(bad).error or "")


@pytest.mark.parametrize("legacy", ["/workspace/a.md", "/skills/x/SKILL.md"])
def test_legacy_mount_paths_are_rejected_with_the_new_one(fs, legacy: str) -> None:
    """旧布局的 /workspace、/skills 直接拒绝，报错里给出新路径。

    ★ 按「其余相对工作区」的规则，它们会悄悄落进工作区里一个叫 workspace 的子目录：模型以为写到了
      工作区根，Shell 里却找不到。
    """
    error = fs.write(legacy, "x").error or ""
    assert "旧的路径" in error and fs.workdir in error


def test_real_paths_and_relative_paths_are_the_same_file(fs) -> None:
    """沙箱里的真实路径 /data/… 和相对工作区的路径指向同一个对象。"""
    fs.write(f"{fs.workdir}/reports/q3.md", "三季度")
    assert fs.read("reports/q3.md").file_data["content"] == "三季度"
    assert fs.key_of("reports/q3.md") == f"{USER}/{fs.layout.workspace}/reports/q3.md"


def test_a_relative_data_directory_is_a_workspace_subdirectory(fs) -> None:
    """★ 相对路径 data/raw.csv 到这里已经被引擎补成 /data/raw.csv：raw.csv 不是布局里的东西
    （用户文件、技能、home），所以按工作区里的 data 子目录算 —— data 是很常见的目录名。"""
    fs.write("/data/raw.csv", "a,b")
    assert fs.key_of("/data/raw.csv") == f"{USER}/{fs.layout.workspace}/data/raw.csv"
    assert fs.read(f"{fs.workdir}/data/raw.csv").file_data["content"] == "a,b"
    assert fs.search("data/").keys == ["data/raw.csv"]
    with pytest.raises(PathEscape, match="越出"):
        fs.key_of("/data/../etc/passwd")


def test_other_roots_under_data_are_not_reachable(fs) -> None:
    """/data 下不是本会话能用的根（工作空间里别的目录、没绑定的技能、CLI 的 home）一律拒绝。"""
    others = (
        "/data/files/other/a.md",
        "/data/files/a.md",
        "/data/skills/x/1/SKILL.md",
        "/data/home/.claude/x",
    )
    for path in others:
        assert "不在本会话能用的目录里" in (fs.read(path).error or "")


def test_null_byte_is_rejected(fs) -> None:
    with pytest.raises(PathEscape):
        fs.key_of("a\x00b")


def test_sessions_cannot_reach_each_other(two_sessions) -> None:
    """跨会话隔离：A 写的文件，B 用任何形态的路径都读不到。"""
    a, b = two_sessions
    a.write("secret.txt", "A 的内容")

    assert b.read("secret.txt").error is not None
    # 试图用相对路径横向穿越
    assert b.read("../t-a/workspace/secret.txt").error is not None
    assert b.search("").keys == []


# ──────────────────────────────────────────────── 读写


def test_write_then_read_roundtrip(fs) -> None:
    fs.write("reports/q3.md", "line1\nline2\nline3\n")
    result = fs.read("reports/q3.md")
    assert result.error is None
    assert "line2" in result.file_data["content"]


def test_read_missing_file_reports_error(fs) -> None:
    result = fs.read("nope.txt")
    assert result.error is not None and "not found" in result.error


def test_read_paginates_by_line(fs) -> None:
    fs.write("big.txt", "\n".join(f"line{i}" for i in range(100)))
    page = fs.read("big.txt", offset=10, limit=5)
    assert page.error is None
    lines = page.file_data["content"].splitlines()
    assert lines == ["line10", "line11", "line12", "line13", "line14"]
    assert page.next_offset == 15


def test_oversized_read_points_at_execute(fs) -> None:
    """超限不截断，而是给出可执行的替代方案。

    截断是丢信息，而报错最常出现在文件末尾 —— 静默截断会让模型
    读到半截然后下错结论。
    """
    fs.write("huge.log", "x" * (READ_MAX_BYTES + 1))
    result = fs.read("huge.log")
    assert result.error is not None
    assert "execute" in result.error and "sed" in result.error


def test_write_overwrites(fs) -> None:
    """对象存储没有追加语义 —— 同路径即覆盖。"""
    fs.write("a.txt", "第一版")
    fs.write("a.txt", "第二版")
    assert fs.read("a.txt").file_data["content"] == "第二版"


# ──────────────────────────────────────────────── edit：read-modify-write


def test_edit_replaces_single_occurrence(fs) -> None:
    fs.write("cfg.yaml", "mode: draft\nname: x\n")
    result = fs.edit("cfg.yaml", "draft", "published")
    assert result.error is None and result.occurrences == 1
    assert "published" in fs.read("cfg.yaml").file_data["content"]


def test_edit_ambiguous_match_refuses(fs) -> None:
    """多处命中且没说 replace_all —— 拒绝，而不是猜第一处。"""
    fs.write("a.txt", "x\nx\nx\n")
    result = fs.edit("a.txt", "x", "y")
    assert result.error is not None and "3 times" in result.error
    assert fs.read("a.txt").file_data["content"] == "x\nx\nx\n"


def test_edit_replace_all(fs) -> None:
    fs.write("a.txt", "x\nx\nx\n")
    result = fs.edit("a.txt", "x", "y", replace_all=True)
    assert result.occurrences == 3
    assert fs.read("a.txt").file_data["content"] == "y\ny\ny\n"


def test_edit_missing_string_reports_error(fs) -> None:
    fs.write("a.txt", "hello")
    assert fs.edit("a.txt", "nope", "x").error is not None


def test_oversized_edit_points_at_sed(fs) -> None:
    """edit 必须取回全文 —— 超限要明确报错，不能静默吞内存。"""
    fs.write("huge.txt", "y" * (EDIT_MAX_BYTES + 1))
    result = fs.edit("huge.txt", "y", "z")
    assert result.error is not None
    assert "sed" in result.error and "in place" in result.error


# ──────────────────────────────────────────────── search


def test_search_lists_one_level_with_common_prefixes(fs) -> None:
    """对象存储没有目录 —— 子目录靠 delimiter 分组模拟出来。"""
    fs.write("a.txt", "1")
    fs.write("reports/q3.md", "2")
    fs.write("reports/q4.md", "3")

    result = fs.search("")
    assert result.keys == ["a.txt"]
    assert result.common_prefixes == ["reports/"]


def test_search_with_prefix(fs) -> None:
    fs.write("reports/q3.md", "1")
    fs.write("reports/q4.md", "2")
    fs.write("other/x.md", "3")

    result = fs.search("reports/")
    assert sorted(result.keys) == ["reports/q3.md", "reports/q4.md"]


def test_search_pattern_filters_by_name(fs) -> None:
    fs.write("reports/q3.md", "1")
    fs.write("reports/data.csv", "2")

    result = fs.search("reports/", pattern="*.md")
    assert result.keys == ["reports/q3.md"]


def test_search_double_star_recurses(fs) -> None:
    """`**` 形态不带 delimiter，跨层匹配。"""
    fs.write("a/b/c/deep.csv", "1")
    fs.write("a/top.csv", "2")
    fs.write("a/b/skip.md", "3")

    result = fs.search("", pattern="**/*.csv")
    assert sorted(result.keys) == ["a/b/c/deep.csv", "a/top.csv"]


def test_search_truncates_and_says_so(fs) -> None:
    """撞上限要标出来 —— 调用方据此提示模型缩小范围。"""
    for i in range(20):
        fs.write(f"bulk/f{i:02d}.txt", "x")

    result = fs.search("bulk/", max_keys=5)
    assert len(result.keys) == 5
    assert result.truncated is True


def test_search_empty_workspace_is_not_an_error(fs) -> None:
    """空结果不是错误 —— 调用方不该为「没找到」写判空分支。"""
    result = fs.search("")
    assert result.error is None and result.keys == [] and result.truncated is False


# ──────────────────────────────────────────────── delete


def test_delete_single_object(fs) -> None:
    fs.write("a.txt", "1")
    assert fs.delete("a.txt").error is None
    assert fs.read("a.txt").error is not None


def test_delete_missing_reports_error(fs) -> None:
    assert fs.delete("nope.txt").error is not None


def test_delete_recursive_removes_whole_prefix(fs) -> None:
    fs.write("junk/a.txt", "1")
    fs.write("junk/nested/b.txt", "2")
    fs.write("keep.txt", "3")

    assert fs.delete("junk", recursive=True).error is None
    assert fs.search("", pattern="**/*").keys == ["keep.txt"]


def test_delete_recursive_on_empty_prefix_reports_error(fs) -> None:
    """删一个不存在的前缀要报错，而不是静默成功 —— 否则模型以为清理完成了。"""
    assert fs.delete("nothing-here", recursive=True).error is not None


# ──────────────────────────────────────────────── 异步包装


async def test_async_wrappers_delegate(fs) -> None:
    await fs.awrite("a.txt", "hello")
    assert (await fs.aread("a.txt")).file_data["content"] == "hello"
    assert (await fs.asearch("")).keys == ["a.txt"]
    assert (await fs.aedit("a.txt", "hello", "bye")).occurrences == 1
    assert (await fs.adelete("a.txt")).error is None


# ──────────────────────────────────────────────── 隐藏前缀与系统区


def test_search_skips_dot_prefixes_by_default(fs) -> None:
    """点开头的段默认不出现在列举里。

    工作区里会长出工具性的隐藏目录（`.git`、`.venv`、构建缓存）。不过滤的
    话模型每次 search("") 都会被它们淹没，看不见自己的产物。
    """
    fs.write("report.md", "我的产物")
    fs.write(".cache/build/a.o", "缓存")
    fs.write(".cache/build/b.o", "缓存")

    result = fs.search("")
    assert result.keys == ["report.md"]
    assert ".cache/" not in result.common_prefixes


def test_search_shows_hidden_when_prefix_is_explicit(fs) -> None:
    """显式给出点开头的前缀就是要看它 —— 此时不再过滤。"""
    fs.write(".cache/build/a.o", "缓存")
    # common_prefixes 是完整相对路径（与 search("") 返回 "reports/" 一致）
    assert fs.search(".cache/").common_prefixes == [".cache/build/"]
    assert fs.search(".cache/build/").keys == [".cache/build/a.o"]


def test_skills_are_not_in_the_workspace_at_all(s3) -> None:
    """技能不靠「隐藏」躲开列举 —— 它在这个人的技能目录 {user}/skills/ 下，不在工作区里。

    文件工具只放行本智能体绑定的版本；结果以沙箱里的真实路径回来。
    """
    fs = Workspace(
        s3,
        bucket=BUCKET,
        user_id=USER,
        thread_id=uuid4(),
        workspace_dir="w",
        skills=[("dataviz", 1)],
    )
    fs.write("report.md", "我的产物")
    fs.write("/data/skills/dataviz/1/SKILL.md", "技能")

    assert fs.key_of("/data/skills/dataviz/1/SKILL.md") == f"{USER}/skills/dataviz/1/SKILL.md"

    # search("") 只看 workspace —— 技能不在其中
    assert fs.search("").keys == ["report.md"]
    assert fs.search("").common_prefixes == []

    # 显式给技能目录才看得到，路径以沙箱里的形态回来
    assert fs.search("/data/skills/dataviz/1/").keys == ["/data/skills/dataviz/1/SKILL.md"]
    assert fs.read("/data/skills/dataviz/1/SKILL.md").file_data["content"].strip() == "技能"


def test_subagent_shares_parent_workspace(s3) -> None:
    """子会话用根会话的工作目录：父写的子看得到，子写的父也看得到。

    这一条红了意味着子智能体看不见父 agent 的产物（协作断了）。
    """
    parent = Workspace(
        s3, bucket=BUCKET, user_id=USER, thread_id="t-parent", workspace_dir="Q3 分析"
    )
    child = Workspace(
        s3, bucket=BUCKET, user_id=USER, thread_id="t-child", workspace_dir="Q3 分析"
    )

    assert child.workdir == parent.workdir == "/data/files/Q3 分析"  # 同一份工作区
    assert child.layout.atlas_dir != parent.layout.atlas_dir  # 隐藏目录各自一个

    parent.write("shared.md", "父写的")
    assert child.read("shared.md").file_data["content"].strip() == "父写的"

    child.write("child.md", "子写的")
    assert parent.read("child.md").file_data["content"].strip() == "子写的"


def test_hidden_is_judged_per_segment(fs) -> None:
    """按**段**判断而不是只看开头 —— 否则 reports/.draft.md 会漏网。"""
    fs.write("reports/.draft.md", "草稿")
    fs.write("reports/final.md", "定稿")
    assert fs.search("reports/").keys == ["reports/final.md"]


def test_offloads_land_in_the_sessions_hidden_dir(fs) -> None:
    """外置的大结果 / 被压缩的历史：写进所用工作区下本会话的隐藏目录 .atlas/{会话}/。

    模型照给它的路径读回来；search("") 默认跳过隐藏目录，不会被它们淹没；界面的文件面板也不列。
    """
    from atlas_agent.providers.filesystem import WorkspaceListing

    atlas = fs.layout.real(fs.layout.atlas_dir)
    fs.write("report.md", "我的产物")
    fs.write(f"{atlas}/large_tool_results/call-1", "很长的结果")

    assert fs.key_of(f"{atlas}/large_tool_results/call-1") == (
        f"{USER}/{fs.layout.workspace}/.atlas/{fs.layout.thread_id}/large_tool_results/call-1"
    )
    assert fs.read(f"{atlas}/large_tool_results/call-1").file_data["content"] == "很长的结果"
    assert fs.search("").keys == ["report.md"]
    assert fs.search("").common_prefixes == []
    assert [f["path"] for f in WorkspaceListing(fs).list()[0]] == ["report.md"]


def test_subagent_and_parent_share_one_skill_copy(s3) -> None:
    """技能一个人一份：父、子会话绑同一版本时只拷一次，用的是同一份。"""
    import asyncio

    from atlas_agent.providers.filesystem.skill_copy import SkillMeta, seed_session_skills

    s3.put_object(Bucket=BUCKET, Key="_skills/dataviz/1/SKILL.md", Body=b"v1")

    parent = Workspace(
        s3,
        bucket=BUCKET,
        user_id=USER,
        thread_id="t-parent",
        workspace_dir="d",
        skills=[("dataviz", 1)],
    )
    child = Workspace(
        s3,
        bucket=BUCKET,
        user_id=USER,
        thread_id="t-child",
        workspace_dir="d",
        skills=[("dataviz", 1)],
    )
    metas = [SkillMeta("dataviz", 1, "画图")]
    asyncio.run(seed_session_skills(parent, metas))
    asyncio.run(seed_session_skills(child, metas))

    keys = {o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=USER)["Contents"]}
    assert f"{USER}/skills/dataviz/1/SKILL.md" in keys
    assert not any("/t-parent/" in k or "/t-child/" in k for k in keys)

    child.write("/data/skills/dataviz/1/SKILL.md", "子智能体改过的")
    assert parent.read("/data/skills/dataviz/1/SKILL.md").file_data["content"] == "子智能体改过的"
