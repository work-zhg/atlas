from __future__ import annotations

from pathlib import Path

import pytest
from atlas_bridge.config import BridgeConfig
from pydantic import ValidationError


def test_from_env() -> None:
    config = BridgeConfig.from_env(
        {
            "ATLAS_BRIDGE_SESSION_ID": "hs-1",
            "ATLAS_BRIDGE_AGENT_CMD": '["npx", "claude-code-acp", "--flag=a b"]',
            "ATLAS_BRIDGE_AGENT_UID": "1001",
            "ATLAS_BRIDGE_AGENT_GID": "",
            "ATLAS_BRIDGE_AGENT_ENV_ALLOW": "PATH, ANTHROPIC_API_KEY,",
            "ATLAS_BRIDGE_LISTEN_PORT": "9000",
            "ATLAS_BRIDGE_CANCEL_GRACE_S": "20",
            "UNRELATED": "x",
        }
    )
    assert config.agent_cmd == ("npx", "claude-code-acp", "--flag=a b")
    assert (config.agent_uid, config.agent_gid) == (1001, None)
    assert config.agent_env_allow == ("PATH", "ANTHROPIC_API_KEY")
    assert config.listen_port == 9000 and config.cancel_grace_s == 20
    assert config.token_file == Path("/run/secrets/bridge-token")


def test_shell_style_command() -> None:
    config = BridgeConfig.from_env(
        {"ATLAS_BRIDGE_SESSION_ID": "s", "ATLAS_BRIDGE_AGENT_CMD": "codex-acp --model 'gpt x'"}
    )
    assert config.agent_cmd == ("codex-acp", "--model", "gpt x")


def test_required_fields() -> None:
    with pytest.raises(ValidationError):
        BridgeConfig.from_env({"ATLAS_BRIDGE_AGENT_CMD": "x"})


def test_agent_env_is_a_whitelist() -> None:
    config = BridgeConfig(session_id="s", agent_cmd=("x",), agent_env_allow=("PATH", "MISSING"))
    assert config.agent_env({"PATH": "/bin", "ATLAS_BRIDGE_SECRET": "no"}) == {"PATH": "/bin"}


def test_token_file(tmp_path: Path) -> None:
    (tmp_path / "t").write_text("  abc\n")
    config = BridgeConfig(session_id="s", agent_cmd=("x",), token_file=tmp_path / "t")
    assert config.read_token() == "abc"
    (tmp_path / "t").write_text("\n")
    with pytest.raises(ValueError, match="为空"):
        config.read_token()
