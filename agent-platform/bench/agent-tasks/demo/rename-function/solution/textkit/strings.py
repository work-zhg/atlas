import re


def slugify(text: str) -> str:
    """把标题转成 URL 片段：小写、非字母数字换成 -。"""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def title_case(text: str) -> str:
    return " ".join(w.capitalize() for w in text.split())
