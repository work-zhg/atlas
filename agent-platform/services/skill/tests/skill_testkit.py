"""技能应用测试的构造工具（不叫 helpers：避免与别处同名模块撞车）。"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any


def skill_files(
    name: str = "demo",
    description: str = "演示技能：用于测试发布流程。不用于真实任务。",
    *,
    extra: dict[str, bytes] | None = None,
    body: str = "# Demo\n\n按步骤做事。\n",
) -> dict[str, bytes]:
    files = {
        "SKILL.md": f"---\nname: {name}\ndescription: {description}\n---\n\n{body}".encode(),
    }
    files.update(extra or {})
    return files


def make_zip(files: dict[str, bytes], *, root: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for path, data in files.items():
            archive.writestr(f"{root}/{path}" if root else path, data)
    return buf.getvalue()


def write_dir(tmp: Path, files: dict[str, bytes]) -> Path:
    for path, data in files.items():
        target = tmp / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return tmp


# ───────────────────────────────────────────── 租户、运营中心

#: 没指定租户的都在这个租户里（conftest 的夹具默认进入它、HTTP 客户端默认带它）
TEST_TENANT = "00000000-0000-0000-0000-00000000000a"
OTHER_TENANT = "00000000-0000-0000-0000-00000000000b"
#: 运营接口的签名密钥（conftest 写进 ATLAS_SKILL_OPS_API_KEY）
OPS_KEY = "test-ops-key"


def ops_signed(request: Any) -> Any:
    """给一个已构造好的 httpx.Request 加上运营中心的签名和操作人（请求体按实际字节签）。"""
    from uuid import uuid4

    from atlas_opsapi import OperatorStamp, sign

    raw = request.read()
    path = request.url.raw_path.decode()
    request.headers.update(sign(OPS_KEY.encode(), request.method, path, raw))
    request.headers.update(
        OperatorStamp(operator_name="值班", idempotency_key=uuid4()).to_headers()
    )
    return request
