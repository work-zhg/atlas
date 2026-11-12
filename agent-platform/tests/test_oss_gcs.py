"""Workspace 对着**真对象存储**（这里是 GCS 的 S3 兼容 XML API）。

没配就整体跳过 —— 与 tests/test_cluster_k8s.py 同款门控。要跑的话在仓库根
.env 里配好：

    OSS_ENDPOINT=https://storage.googleapis.com
    OSS_BUCKET=<你的桶>
    OSS_ACCESS_KEY_ID=<HMAC key>
    OSS_ACCESS_KEY_SECRET=<HMAC secret>
    OSS_REGION=auto
    OSS_ADDRESSING_STYLE=path

## 为什么这组测试不可省

其余用例跑在 moto 上，覆盖的是**主体逻辑**（前缀路由、分页、逃逸吸收）。
但 moto 是个宽容的模拟器：它不校验签名、不实现 delimiter 的全部语义、
对多余的请求头照单全收。第一次对着真桶写文件就抓到一个它掩盖的 bug ——

    botocore >= 1.36 默认给 PutObject 带上 CRC32 校验和头，而 GCS 的 XML API
    不认这套流程，签名对不上，报的却是 **SignatureDoesNotMatch**。
    错误指向凭据而不是指向真因，排查会先去翻 key —— 而 key 是好的。

这和上一轮 InMemoryBackend 掩盖三个 K8s bug 是同一件事：模拟器测的是
「我以为的协议」，真服务测的是「协议本身」。

## 隔离

每条用例用自己的 thread id 前缀，结束时连 workspace 带 skills 一起删。
共用一个桶是安全的 —— 前缀天然就是命名空间。
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from atlas_agent.config import Settings
from atlas_agent.providers.filesystem import PathEscape, make_workspace

from .conftest import REPO_ROOT


def _real_settings() -> Settings | None:
    """显式把仓库根 .env 读回来。

    ★ 全局夹具特意把对象存储那几个键从测试可见的 env 里摘掉了（见 conftest），
      所以这里必须显式选择进来 —— 「对着真桶跑」是一个 opt-in，不该是默认。
    """
    env = REPO_ROOT / ".env"
    if not Path(env).exists():
        return None
    try:
        settings = Settings(_env_file=env)
    except Exception:
        return None
    if not (settings.workspace_configured and settings.oss_endpoint):
        return None
    return settings


_SETTINGS = _real_settings()

pytestmark = pytest.mark.skipif(
    _SETTINGS is None,
    reason="未配置真对象存储（.env 缺 OSS_ENDPOINT / OSS_BUCKET / HMAC 凭据）",
)


@pytest.fixture
def workspaces():
    """按 thread id 造工作区的工厂，结束时把造过的全部清干净。

    共用一个真桶，所以清理不能漏 —— 残留会跨用例累积，表现是列举类断言
    随机多出几个 key。
    """
    created = []

    def _make(thread_id: str, *, workspace_dir: str | None = None):
        workspace = make_workspace(
            _SETTINGS,
            "test-user",
            thread_id,
            workspace_dir or f"test/{thread_id}",
            skills=[_SKILL],
        )
        assert workspace is not None
        created.append(workspace)
        return workspace

    try:
        yield _make
    finally:
        for workspace in created:
            # ★ 两次 delete：技能在这个人的 skills/ 下，不在工作区里
            workspace.delete(f"{_SKILL_DIR}/", recursive=True)
            workspace.delete("", recursive=True)


#: 用例里用的技能版本（绑在每个工作区上）
_SKILL = (f"gcs-test-{uuid4().hex[:8]}", 1)
_SKILL_DIR = f"/data/skills/{_SKILL[0]}/{_SKILL[1]}"


@pytest.fixture
def fs(workspaces):
    """真桶上的一个独立会话前缀。"""
    return workspaces(f"test-{uuid4()}")


def _keys_under(workspace, prefix: str) -> list[str]:
    return sorted(workspace.search(prefix).keys)


# ──────────────────────────────────────────────── 被 moto 掩盖的那个 bug


def test_writing_a_file_actually_succeeds(fs) -> None:
    """★ 回归钉：PutObject 不带 boto3 默认的校验和头。

    带了的话 GCS 回 SignatureDoesNotMatch —— 整个文件能力一条都用不了，
    而错误信息会把人引去查凭据。moto 不校验签名，所以只有这条能发现它。
    """
    assert fs.write("notes/a.md", "hello").error is None
    assert fs.read("notes/a.md").file_data["content"] == "hello"


def test_the_client_disables_default_checksums(fs) -> None:
    """把原因钉在配置上，而不只是钉在症状上。

    上一条挂了只知道"写不进去"；这条直接指出是哪个开关被改回了默认。
    """
    config = fs._s3.meta.config
    assert config.request_checksum_calculation == "when_required"
    assert config.response_checksum_validation == "when_required"


# ──────────────────────────────────────────────── 双前缀路由


def test_workspace_and_skills_land_on_different_prefixes(fs) -> None:
    """工作区和技能是这个人前缀下两个不同的目录：{user}/files/{工作目录}、{user}/skills/…。

    路由错了的表现不是报错，是技能写进了工作区 —— 一个静默的越权写。
    """
    fs.write("report.md", "工作区")
    fs.write(f"{_SKILL_DIR}/SKILL.md", "技能")

    assert _keys_under(fs, "") == ["report.md"], "技能漏进了工作区列表"
    skill_key = f"test-user{_SKILL_DIR.removeprefix('/data')}/SKILL.md"
    assert fs.key_of(f"{_SKILL_DIR}/SKILL.md") == skill_key
    assert fs.key_of("report.md") == f"test-user/{fs.layout.workspace}/report.md"


def test_deleting_the_workspace_leaves_skills_alone(fs) -> None:
    """清空工作区不该连技能一起清掉 —— 技能是这个人所有会话共用的另一份资产。"""
    fs.write("scratch.txt", "x")
    fs.write(f"{_SKILL_DIR}/SKILL.md", "技能")

    fs.delete("", recursive=True)

    assert _keys_under(fs, "") == []
    assert fs.search(f"{_SKILL_DIR}/").keys == [f"{_SKILL_DIR}/SKILL.md"]


def test_a_subagent_shares_the_parents_workspace_and_the_persons_skills(workspaces) -> None:
    """子会话：工作目录用根会话的；技能一个人一份，父子看到的是同一份。

    共享工作区是有意的（委派常常就是「帮我处理这批文件」，产物要互相看得见）。
    """
    parent_thread = f"test-parent-{uuid4()}"
    parent = workspaces(parent_thread)
    child = workspaces(f"test-child-{uuid4()}", workspace_dir=f"test/{parent_thread}")

    parent.write("parent-note.md", "父写的")
    assert child.read("parent-note.md").file_data["content"] == "父写的"

    child.write("child-note.md", "子写的")
    assert parent.read("child-note.md").file_data["content"] == "子写的"

    parent.write(f"{_SKILL_DIR}/SKILL.md", "父改的技能")
    assert child.read(f"{_SKILL_DIR}/SKILL.md").file_data["content"] == "父改的技能"


# ──────────────────────────────────────────────── 真服务的 delimiter 语义


def test_common_prefixes_are_real_directories(fs) -> None:
    """★ search 的目录感是用 delimiter **模拟**出来的。

    GCS 与 moto 在这里未必逐字一致，而它直接决定模型看到的"目录树"。
    """
    fs.write("reports/q3.md", "a")
    fs.write("reports/q4.md", "b")
    fs.write("archive/old.md", "c")
    fs.write("top.md", "d")

    result = fs.search("")
    assert result.keys == ["top.md"], "深层文件被平铺进了当前层"
    assert sorted(result.common_prefixes) == ["archive/", "reports/"]

    nested = fs.search("reports/")
    assert sorted(nested.keys) == ["reports/q3.md", "reports/q4.md"]


def test_glob_reaches_across_directories(fs) -> None:
    """`**/*.csv` 要能穿透层级 —— 它走的是全量列举而不是 delimiter。"""
    fs.write("a/b/c/deep.csv", "1")
    fs.write("a/top.csv", "2")
    fs.write("a/notes.md", "3")

    assert sorted(fs.search("", pattern="**/*.csv").keys) == ["a/b/c/deep.csv", "a/top.csv"]


# ──────────────────────────────────────────────── 读写边界


def test_reading_pages_through_a_large_file(fs) -> None:
    fs.write("big.txt", "\n".join(f"line{i}" for i in range(500)))

    page = fs.read("big.txt", offset=100, limit=5)
    assert page.file_data["content"].splitlines() == [f"line{i}" for i in range(100, 105)]
    assert page.next_offset == 105


def test_edit_is_a_real_read_modify_write(fs) -> None:
    """edit 在对象存储上没有原子替换可用 —— 它是读回来、改、再整体写。

    真桶上确认这条链路完整：中间任何一步的编码/校验和问题都会在这里现形。
    """
    fs.write("cfg.yaml", "mode: draft\nowner: draft-team\n")

    result = fs.edit("cfg.yaml", "draft", "published", replace_all=True)
    assert result.error is None
    assert result.occurrences == 2
    assert fs.read("cfg.yaml").file_data["content"] == "mode: published\nowner: published-team\n"


def test_non_ascii_survives_the_round_trip(fs) -> None:
    """UTF-8 要原样回来 —— 对象存储不该按平台 locale 解释字节。"""
    fs.write("中文/笔记.md", "带 emoji 的正文 🚀\n第二行")
    assert fs.read("中文/笔记.md").file_data["content"] == "带 emoji 的正文 🚀\n第二行"
    assert _keys_under(fs, "中文/") == ["中文/笔记.md"]


# ──────────────────────────────────────────────── 逃逸


def test_the_workspace_mount_point_is_not_a_subdirectory(fs) -> None:
    """★ 沙箱里的真实路径 `/data/files/{工作目录}/a.md` 指的是工作区根上的 a.md。

    模型看得见这个路径 —— 命令和 CLI 的 cwd 就是它 —— 所以它会很自然地写出绝对路径。
    不认的话对象会落在一层子目录里，而错法是**静默**的：写入成功、事件照发，只是文件出现在
    一个谁也不会去看的地方（旧布局真跑第一轮就撞上过：.../workspace/workspace/domain.md）。
    """
    fs.write(f"{fs.workdir}/absolute.md", "用绝对路径写的")

    # 落在工作区根上，search("") 就看得见 —— 而不是藏在一层子目录里
    assert "absolute.md" in fs.search("").keys
    assert fs.read("absolute.md").file_data["content"] == "用绝对路径写的"
    assert fs.read(f"{fs.workdir}/absolute.md").file_data["content"] == "用绝对路径写的"
    key = f"test-user/{fs.layout.workspace}/absolute.md"
    assert fs.key_of(f"{fs.workdir}/absolute.md") == key


def test_a_relative_workspace_directory_still_means_a_directory(fs) -> None:
    """★ 相对的 `workspace/x` 仍然是子目录 —— 与绝对路径是两个意思。

    两者归一化之后长得一样，但原始入参分得出来，所以不该合并：
    合并了的话，用户真建一个叫 workspace 的目录就再也访问不到。
    """
    fs.write("workspace/nested.md", "这是子目录")

    assert fs.key_of("workspace/nested.md") == (
        f"test-user/{fs.layout.workspace}/workspace/nested.md"
    )
    assert fs.search("").common_prefixes == ["workspace/"]
    assert fs.read("workspace/nested.md").file_data["content"] == "这是子目录"


def test_search_and_write_agree_on_the_mount_prefix(fs) -> None:
    """列举与读写必须对同一个前缀说同一件事。

    不一致的表现是"写进去了但 search 看不见" —— 模型会以为自己没写成功，
    然后重写一遍。
    """
    fs.write(f"{fs.workdir}/reports/q3.md", "季报")

    assert fs.search(f"{fs.workdir}/").common_prefixes == ["reports/"]
    assert fs.search(f"{fs.workdir}/reports/").keys == ["reports/q3.md"]
    assert fs.search("reports/").keys == ["reports/q3.md"]


def test_path_escape_is_rejected_before_it_reaches_the_key_namespace(fs) -> None:
    """★ `../` 越出区根直接拒绝，绝不真的爬出去。

    对象存储的 key 是扁平字符串，`..` 没有特殊含义 —— 所以这条只能靠我们自己的路径关卡。
    漏了的话，一个用户能读写另一个用户的前缀。
    """
    with pytest.raises(PathEscape):
        fs.key_of("../../../other-user/secrets.txt")
    assert "越出" in (fs.write("../../escaped.txt", "x").error or "")
    assert _keys_under(fs, "") == []
