"""配置服务测试的构造工具（不叫 helpers：避免与别处同名模块撞车）。"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path


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
