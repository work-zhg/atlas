"""评测的环境设置。地址与密钥只从环境变量 / 仓库根 .env 读，不进实验配置文件。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["BENCH_DIR", "REPO_ROOT", "BenchSettings", "get_settings"]


def _repo_root() -> Path:
    """仓库根：往上第一个有 uv.lock 的目录 —— 目录层级调整后不用改。
    装进镜像（site-packages）后找不到，退回原来的 parents[3]，行为不变。"""
    here = Path(__file__).resolve()
    return next((p for p in here.parents if (p / "uv.lock").is_file()), here.parents[3])


REPO_ROOT = _repo_root()
#: bench 包自己的目录（datasets/、agent-tasks/ 在这里），不经仓库根拼路径
BENCH_DIR = Path(__file__).resolve().parents[2]


class BenchSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ATLAS_BENCH_",
        env_file=REPO_ROOT / ".env",
        extra="ignore",
        populate_by_name=True,
    )

    # ── 被测平台：Atlas 知识库服务（经网关）────────────────────────────
    atlas_kb_url: str = "http://localhost:8080/atlas-knowledge-api"
    #: 评测专用服务账号的密钥（知识库评测需 kb:create；智能体评测需 agent:create + chat:use）
    atlas_api_key: SecretStr | None = None
    #: 被测平台：atlas-server（智能体评测，doc/agent-eval-design.html）
    atlas_api_url: str = "http://127.0.0.1:8000"

    # ── 生成与裁判模型（OpenAI 兼容接口）─────────────────────────────────
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "ATLAS_BENCH_LLM_API_KEY", "MEMORY_LLM_API_KEY", "LITELLM_KEY"
        ),
    )
    llm_timeout_s: float = 300.0

    # ── 本地目录 ───────────────────────────────────────────────────────
    datasets_dirs: list[Path] = [
        BENCH_DIR / "datasets",
        REPO_ROOT / "var" / "bench" / "datasets",
    ]
    runs_dir: Path = REPO_ROOT / "var" / "bench" / "runs"
    cache_path: Path = REPO_ROOT / "var" / "bench" / "cache.sqlite"


@lru_cache
def get_settings() -> BenchSettings:
    return BenchSettings()
