"""第 2、3 层检查与包元数据（设计 §6.2）。

  第 2 层 结构     SKILL.md、frontmatter、name/description —— blocker
  第 3 层 内容模式 密钥、危险命令、注入特征 —— 只记 warn，交给人工判断
  描述冲突         与已发布技能的描述相似度 —— warn

★ 第 3 层不拦截：模式扫描既会误报（文档里讲解如何防注入，本身就含那些字样），
  也必然漏报（逻辑正确、意图不良的代码）。它的作用是把可疑处指给审查人看，
  不是替审查人做决定。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

import yaml

from ..schemas import SKILL_SLUG_PATTERN
from .package import RawPackage, ScanBlocked

__all__ = ["ParsedSkill", "content_scan", "parse", "similarity", "similarity_scan"]

_SCRIPT_EXT = (".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".ps1")
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(\n|\Z)", re.S)


@dataclass(frozen=True)
class ParsedSkill:
    slug: str
    description: str
    frontmatter: dict[str, Any]
    files: list[dict[str, Any]]
    content_hash: str
    size_bytes: int
    has_scripts: bool


def _is_script(path: str, body: bytes, executable: bool) -> bool:
    return (
        path.startswith("scripts/")
        or path.lower().endswith(_SCRIPT_EXT)
        or body.startswith(b"#!")
        or executable
    )


def content_hash_of(files: list[dict[str, Any]]) -> str:
    """与文件顺序无关：先按路径排序再算。"""
    lines = "".join(f"{f['path']}\0{f['sha256']}\n" for f in sorted(files, key=lambda f: f["path"]))
    return "sha256:" + hashlib.sha256(lines.encode()).hexdigest()


def parse(pkg: RawPackage, *, description_max: int) -> ParsedSkill:
    """第 2 层：结构。不合格即 ScanBlocked。"""
    hits: list[str] = []
    raw = pkg.files.get("SKILL.md")
    frontmatter: dict[str, Any] = {}
    if raw is None:
        raise ScanBlocked("structure", ["包的根目录没有 SKILL.md"])
    text = raw.decode("utf-8", errors="replace")
    match = _FRONTMATTER.match(text)
    if not match:
        hits.append("SKILL.md 开头没有 --- 包围的 frontmatter")
    else:
        try:
            loaded = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            hits.append(f"frontmatter 不是合法 YAML：{exc}")
            loaded = None
        if loaded is not None and not isinstance(loaded, dict):
            hits.append("frontmatter 必须是键值对")
        elif isinstance(loaded, dict):
            frontmatter = loaded

    name = str(frontmatter.get("name") or "").strip()
    description = str(frontmatter.get("description") or "").strip()
    if match and not name:
        hits.append("frontmatter 缺少 name")
    elif name and not re.match(SKILL_SLUG_PATTERN, name):
        hits.append(f"name {name!r} 不合规：只能是小写字母开头的 a-z0-9-，≤63 字符")
    if match and not description:
        hits.append("frontmatter 缺少 description —— 它是模型决定要不要用这个技能的唯一依据")
    elif len(description) > description_max:
        hits.append(f"description {len(description)} 字符，超过上限 {description_max}")
    if hits:
        raise ScanBlocked("structure", hits)

    files = [
        {
            "path": path,
            "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "executable": path in pkg.executable,
        }
        for path, body in sorted(pkg.files.items())
    ]
    return ParsedSkill(
        slug=name,
        description=description,
        # ★ 存进 JSON 列前要能序列化：YAML 里的日期等类型转成字符串
        frontmatter={
            str(k): v if isinstance(v, (str, int, float, bool, list, dict)) else str(v)
            for k, v in frontmatter.items()
        },
        files=files,
        content_hash=content_hash_of(files),
        size_bytes=pkg.size,
        has_scripts=any(_is_script(p, b, p in pkg.executable) for p, b in pkg.files.items()),
    )


# ───────────────────────────────────────────── 第 3 层：内容模式

#: (规则名, 正则, 只查脚本)
_RULES: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    ("私钥", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), False),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), False),
    ("API 密钥（sk-…）", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), False),
    ("Bearer 令牌", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{24,}"), False),
    ("下载即执行", re.compile(r"(?i)\b(curl|wget)\b[^\n|]*\|\s*(ba|z)?sh\b"), False),
    ("删除根目录", re.compile(r"\brm\s+-[a-z]*r[a-z]*f?\s+/(\s|$|\*)"), False),
    ("直连 IP 地址", re.compile(r"https?://\d{1,3}(\.\d{1,3}){3}"), False),
    (
        "提示词注入特征",
        re.compile(
            r"(?i)(ignore\s+(all\s+)?(previous|prior|above)\s+instructions"
            r"|disregard\s+(the\s+)?system\s+prompt|you\s+are\s+now\s+"
            r"|忽略(之前|以上|前面|上述)的?(所有)?(指令|指示|规则)|你现在是)"
        ),
        False,
    ),
    ("执行外部命令", re.compile(r"\b(subprocess|os\.system|os\.popen|child_process)\b"), True),
    ("动态执行代码", re.compile(r"\b(eval|exec)\s*\("), True),
)


def _text(body: bytes) -> str | None:
    if b"\x00" in body[:1024]:
        return None
    return body.decode("utf-8", errors="ignore")


def content_scan(pkg: RawPackage) -> dict[str, Any]:
    hits: list[str] = []
    for path, body in sorted(pkg.files.items()):
        text = _text(body)
        if text is None:
            continue
        script = _is_script(path, body, path in pkg.executable)
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, pattern, scripts_only in _RULES:
                if scripts_only and not script:
                    continue
                if pattern.search(line):
                    hits.append(f"{path}:{lineno} {name}")
    return {"result": "warn" if hits else "pass", "hits": hits[:200]}


# ───────────────────────────────────────────── 描述冲突


def _grams(text: str) -> set[str]:
    norm = re.sub(r"\s+", " ", text.lower()).strip()
    return {norm[i : i + 3] for i in range(max(len(norm) - 2, 1))}


def similarity(a: str, b: str) -> float:
    """字符 3-gram 的 Jaccard。中英文都适用，不引分词依赖。"""
    ga, gb = _grams(a), _grams(b)
    return len(ga & gb) / len(ga | gb) if ga and gb else 0.0


def similarity_scan(
    description: str, others: dict[str, str], *, threshold: float
) -> dict[str, Any]:
    hits = [
        f"与 {slug} 的描述相似度 {score:.2f}"
        for slug, text in sorted(others.items())
        if (score := similarity(description, text)) >= threshold
    ]
    return {"result": "warn" if hits else "pass", "hits": hits}
