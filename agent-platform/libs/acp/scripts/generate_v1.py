"""从收录的官方 schema 生成 ACP v1 的 pydantic 模型（代码设计 §3）。

    uv run python acp/scripts/generate_v1.py

输入：acp/schema/v1/schema.json、meta.json（固定在某个发布标签，见 SOURCE.md）
输出：acp/src/atlas_acp/v1/_generated.py、acp/src/atlas_acp/v1/methods.py

★ 不访问网络、不写时间戳：同样的输入永远得到同样的输出，升级 ACP 时的差异一目了然。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema" / "v1" / "schema.json"
META = ROOT / "schema" / "v1" / "meta.json"
OUT_DIR = ROOT / "src" / "atlas_acp" / "v1"
TAG = "schema-v1.23.0"


def _header(extra: str = "") -> str:
    digest = hashlib.sha256(SCHEMA.read_bytes()).hexdigest()
    return (
        f"# 由 acp/scripts/generate_v1.py 从官方 ACP schema（{TAG}，sha256 {digest[:16]}…）生成。\n"
        "# 不要手改：升级 ACP 时更新 acp/schema/v1/ 后重新运行生成脚本。\n" + extra
    )


def generate_models() -> None:
    out = OUT_DIR / "_generated.py"
    cmd = [
        sys.executable, "-m", "datamodel_code_generator",
        "--input", str(SCHEMA),
        "--input-file-type", "jsonschema",
        "--output", str(out),
        "--output-model-type", "pydantic_v2.BaseModel",
        "--target-python-version", "3.13",
        "--use-standard-collections",
        "--use-union-operator",
        "--collapse-root-models",      # ToolCallId 这类简单别名展开成 str，不生成包装类
        "--snake-case-field",          # Python 里是 snake_case，线上仍是 camelCase（别名）
        "--allow-population-by-field-name",
        "--allow-extra-fields",        # ★ 新版本 agent 多带的字段不能丢（§3.8 B2）
        "--use-schema-description",
        "--use-double-quotes",
        "--disable-timestamp",
        "--custom-file-header", _header(),
    ]  # fmt: skip
    subprocess.run(cmd, check=True)
    print(f"已生成 {out.relative_to(ROOT)}")


def generate_methods() -> None:
    meta = json.loads(META.read_text())
    lines = [
        _header(),
        '"""ACP v1 的方法名（来自官方 meta.json）。"""',
        "",
        "from __future__ import annotations",
        "",
    ]
    groups = {
        "agentMethods": "client → agent",
        "clientMethods": "agent → client",
        "protocolMethods": "双向",
    }
    for key, direction in groups.items():
        lines.append(f"# {direction}")
        for const, method in meta[key].items():
            lines.append(f'{const.upper()} = "{method}"')
        lines.append("")
    lines.append(f"PROTOCOL_VERSION = {meta['version']}")
    out = OUT_DIR / "methods.py"
    out.write_text("\n".join(lines) + "\n")
    print(f"已生成 {out.relative_to(ROOT)}")


if __name__ == "__main__":
    generate_models()
    generate_methods()
