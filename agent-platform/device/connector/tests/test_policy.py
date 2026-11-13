"""本机策略：权限模式上限、开会话前的核对、项目目录。"""

from __future__ import annotations

from pathlib import Path

import pytest
from atlas_connect.config import DEFAULT_TOML, LocalPolicy
from atlas_connect.session import ProjectDir, Rejected
from cn_kit import policy


def test_default_config_parses() -> None:
    import tomllib

    p = LocalPolicy.parse(tomllib.loads(DEFAULT_TOML))
    assert p.allowed_roots == ("~/atlas-projects",)
    assert p.clis["claude-code"].command == ("claude-agent-acp",)


@pytest.mark.parametrize(
    ("cap", "mode", "allowed"),
    [
        ("auto", "auto", True),
        ("acceptEdits", "auto", False),
        ("acceptEdits", "acceptEdits", True),
        ("acceptEdits", "default", True),
        ("default", "plan", True),
        ("plan", "default", False),
        ("auto", "bypassPermissions", False),  # 不认识的不放
    ],
)
def test_mode_cap(cap: str, mode: str, allowed: bool) -> None:
    assert LocalPolicy(max_mode=cap).mode_allowed(mode) is allowed


def test_check_open_rejects_with_reasons(tmp_path: Path) -> None:
    p = policy(tmp_path, max_mode="acceptEdits", max_sessions=1)
    ok = {"cli_type": "claude-code", "mode": "accept_edits"}
    assert p.check_open(ok, running=0) is None
    assert p.check_open({**ok, "cli_type": "codex"}, running=0).reason == "cli_not_configured"
    too_high = p.check_open({**ok, "mode": "auto"}, running=0)
    assert too_high.reason == "mode_too_high" and "接受编辑" in too_high.message
    assert p.check_open(ok, running=1).reason == "too_many_sessions"


def test_project_dir_must_be_inside_allowed_roots(tmp_path: Path) -> None:
    root = tmp_path / "code"
    (root / "shop").mkdir(parents=True)
    outside = tmp_path / "secret"
    outside.mkdir()
    p = policy(root)
    assert ProjectDir.resolve(str(root / "shop"), p, create=False) == (root / "shop").resolve()
    with pytest.raises(Rejected) as info:
        ProjectDir.resolve(str(outside), p, create=False)
    assert info.value.reason == "dir_not_allowed"


def test_symlink_out_of_the_roots_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "code"
    root.mkdir()
    secret = tmp_path / "ssh"
    secret.mkdir()
    (root / "link").symlink_to(secret)
    with pytest.raises(Rejected):
        ProjectDir.resolve(str(root / "link"), policy(root), create=False)


def test_default_dir_is_created_but_chosen_dir_must_exist(tmp_path: Path) -> None:
    p = policy(tmp_path)
    with pytest.raises(Rejected) as info:
        ProjectDir.resolve(str(tmp_path / "nope"), p, create=False)
    assert info.value.reason == "dir_not_found"
    made = ProjectDir.resolve(str(tmp_path / "3f2a"), p, create=True)
    assert made.is_dir()
