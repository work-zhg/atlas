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

    # ── 被测平台（内网身份传递 §12）─────────────────────────────────────
    #: 内网接口不对宿主暴露：smoke.sh / regress.sh 先 kubectl port-forward 到这两个端口
    #: 知识库服务（atlas-knowledge）
    atlas_kb_url: str = "http://127.0.0.1:18050"
    #: atlas-server（智能体评测，doc/agent-eval-design.html）
    atlas_api_url: str = "http://127.0.0.1:18000"
    #: 评测身份：用户中心里的普通用户 atlas-bench（禁止登录），授「评测」角色
    #: （kb:create · agent:create · chat:use）。请求头 X-Uid 写它，X-Caller-App: bench
    uid: str | None = None
    #: 它所在的租户（单租户部署可不填：用户中心按 uid 推）
    tenant_id: str | None = None

    def identity_headers(self) -> dict[str, str]:
        if not self.uid:
            raise SystemExit(
                "缺少评测身份：设置 ATLAS_BENCH_UID（用户中心里的评测用户 atlas-bench 的 id）"
            )
        h = {"X-Uid": self.uid, "X-Caller-App": "bench"}
        if self.tenant_id:
            h["X-Tenant-Id"] = self.tenant_id
        return h

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
