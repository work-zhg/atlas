"""OssFilesystem 的行为契约。

用 moto 的内存 S3 —— 与真实 OSS / MinIO 是同一套 S3 API，所以这些断言
在三种环境上等价成立。

最重要的一组是 `test_path_escapes_are_rejected`：会话隔离全靠
`_to_key()`，它错了等于会话之间可以互相读写，而从调用方看毫无异常。
"""

from __future__ import annotations

from uuid import uuid4

import boto3
import pytest
from atlas_engine.contracts import EDIT_MAX_BYTES, FilesystemProtocol
from moto import mock_aws

from atlas_server.providers.filesystem.oss import (
    READ_MAX_BYTES,
    OssFilesystem,
    PathEscape,
)

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
    return OssFilesystem(s3, bucket=BUCKET, user_id=USER, thread_id=uuid4())


@pytest.fixture
def two_sessions():
    """同一个桶里的两个会话 —— 用来验证隔离不是靠约定。"""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        a = OssFilesystem(s3, bucket=BUCKET, user_id=USER, thread_id="t-a")
        b = OssFilesystem(s3, bucket=BUCKET, user_id=USER, thread_id="t-b")
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
def test_path_escapes_are_absorbed_not_followed(fs, escape: str) -> None:
    """逃逸路径被归一进工作区，绝不指向工作区之外。

    ★ 规范化必须在拼前缀之前做。先拼后归一的话这些路径会落到
      别的会话（甚至别的 root）上，而调用方拿到的是一个成功的 WriteResult。
    """
    key = fs._to_key(escape)
    assert key.startswith(f"{USER}/"), key
    assert "/../" not in key and not key.endswith("/..")
    # 归一之后必须仍在本会话的 workspace 下
    assert "/workspace/" in key


@pytest.mark.parametrize("bad", ["", "/", ".", "./", "/.."])
def test_root_itself_is_not_a_file(fs, bad: str) -> None:
    """工作区根不是文件 —— 读写它必须显式失败而不是落到某个空 key 上。"""
    with pytest.raises(PathEscape):
        fs._to_key(bad)


def test_null_byte_is_rejected(fs) -> None:
    with pytest.raises(PathEscape):
        fs._to_key("a\x00b")


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


def test_skills_are_not_in_the_workspace_at_all(fs) -> None:
    """技能不靠「隐藏」躲开列举 —— 它根本不在 workspace 前缀下。

    ★ 这是结构性的，不是约定：workspace 是**共享挂载**的（子智能体的 Pod
      挂父 agent 的 workspace）。技能若住在里面，挂载会连父的技能一起带上，
      而子智能体应该有自己的一套。平级之后挂载根本带不上。
    """
    fs.write("report.md", "我的产物")
    fs.write("/skills/dataviz/SKILL.md", "技能")

    # 落到两个不同的绝对前缀
    assert fs._ws != fs._skills
    assert fs._skills.endswith("/skills")

    # search("") 只看 workspace —— 技能不在其中
    assert fs.search("").keys == ["report.md"]
    assert fs.search("").common_prefixes == []

    # 显式走挂载点才看得到，路径以模型侧的形态回来
    assert fs.search("/skills/").common_prefixes == ["skills/dataviz/"]
    assert fs.search("/skills/dataviz/").keys == ["skills/dataviz/SKILL.md"]
    assert fs.read("/skills/dataviz/SKILL.md").file_data["content"].strip() == "技能"


def test_subagent_shares_parent_workspace_but_keeps_own_skills(s3) -> None:
    """子会话的挂载形态：workspace 是父的，skills 是自己的。

    这一条红了意味着子智能体要么看不见父 agent 的产物（协作断了），
    要么读到了父的技能（隔离断了）—— 两个方向都是回归。
    """
    parent = OssFilesystem(s3, bucket=BUCKET, user_id=USER, thread_id="t-parent")
    child = OssFilesystem(
        s3,
        bucket=BUCKET,
        user_id=USER,
        thread_id="t-child",
        workspace_thread_id="t-parent",
    )

    assert child._ws == parent._ws            # 同一份工作区
    assert child._skills != parent._skills    # 各自的技能

    parent.write("shared.md", "父写的")
    assert child.read("shared.md").file_data["content"].strip() == "父写的"

    child.write("child.md", "子写的")
    assert parent.read("child.md").file_data["content"].strip() == "子写的"

    parent.write("/skills/a/SKILL.md", "父的技能")
    assert child.search("/skills/").common_prefixes == []


def test_hidden_is_judged_per_segment(fs) -> None:
    """按**段**判断而不是只看开头 —— 否则 reports/.draft.md 会漏网。"""
    fs.write("reports/.draft.md", "草稿")
    fs.write("reports/final.md", "定稿")
    assert fs.search("reports/").keys == ["reports/final.md"]


def test_system_area_is_sibling_of_workspace(fs) -> None:
    """系统区与 workspace **平级**，不在它下面。

    压缩归档与大结果外置放进工作区的话，模型 search 时会被它们淹没。
    平级之后它们对文件工具结构性不可见 —— 不是靠过滤，是根本不在前缀内。
    """
    assert fs._sys.endswith("/system")
    assert not fs._sys.startswith(fs._ws)
    assert fs._ws.rsplit("/", 1)[0] == fs._sys.rsplit("/", 1)[0]


def test_subagent_skills_land_under_its_own_thread_prefix(s3) -> None:
    """子智能体的技能拷进**自己的** thread 前缀，改它不影响主 agent。

    ★ 设计 §11 步骤 1 的验收点。红了意味着「技能完全隔离」退化成了约定：
      共享挂载会把主 agent 的技能一起带给子智能体，而它应该有自己的一套。
    """
    import asyncio

    from atlas_server.providers.filesystem.skill_copy import SkillMeta, seed_session_skills

    s3.put_object(Bucket=BUCKET, Key="_skills/dataviz/1/SKILL.md", Body=b"v1")

    parent = OssFilesystem(s3, bucket=BUCKET, user_id=USER, thread_id="t-parent")
    child = OssFilesystem(
        s3,
        bucket=BUCKET,
        user_id=USER,
        thread_id="t-child",
        workspace_thread_id="t-parent",
    )
    metas = [SkillMeta("dataviz", 1, "画图")]
    asyncio.run(seed_session_skills(parent, metas))
    asyncio.run(seed_session_skills(child, metas))

    # 同一个技能，两份副本，落在两个 thread 前缀下
    keys = {
        o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=USER)["Contents"]
    }
    assert f"{USER}/t-parent/skills/dataviz/SKILL.md" in keys
    assert f"{USER}/t-child/skills/dataviz/SKILL.md" in keys

    # 子智能体改自己那份，主 agent 的不受影响
    child.write("/skills/dataviz/SKILL.md", "子智能体改过的")
    assert parent.read("/skills/dataviz/SKILL.md").file_data["content"].strip() == "v1"
