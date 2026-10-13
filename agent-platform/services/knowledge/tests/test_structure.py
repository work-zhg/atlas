"""结构感知分段（rag/structure.py）：按标题切小节、父块打包、子块与前缀。"""

from __future__ import annotations

from atlas_knowledge.rag.index_processor import normalize_process_rule, transform
from atlas_knowledge.rag.models import Document
from atlas_knowledge.rag.structure import (
    StructureOptions,
    build_parents,
    child_texts,
    parse_sections,
    path_text,
)

RESUME = """# 张三

**男｜30 岁｜上海**

---

## 个人优势

**核心能力**

- 从 0 到 1 搭建平台：能独立完成产品定义到上线的闭环。
- 技术领导力：长期担任项目技术负责人，负责新人带教与 Code Review。

## 专业技能

| 方向 | 技能 |
| --- | --- |
| 后端 | Java、Spring Boot、Python |
| 中间件 | Kafka、Redis |

## 项目经验

### 一、CI/CD 平台｜主导

- 组件版本治理：基于 Maven 聚合 pom 统一托管版本。
- 微环境：需求级 CI/CD 隔离，按需拉起依赖服务。

#### 技术要点

- 流水线模板化编排。

### 二、资信平台｜技术经理

为风控决策提供统一的第三方资信数据服务：业务方只凭 uid 取数，平台负责对接各家资信机构。

## 教育背景

某大学｜通信工程｜本科
"""


def _parents(text: str, **kw: int):  # type: ignore[no-untyped-def]
    opts = StructureOptions(**kw)
    return build_parents(parse_sections(text, opts.max_heading_level), opts), opts


def test_sections_follow_headings() -> None:
    secs = parse_sections(RESUME, 3)
    paths = [path_text(s.path) for s in secs]
    # 只有标题的「项目经验」不单独成小节；它的标题留在下级的路径里
    assert "张三 > 项目经验" not in paths
    assert "张三 > 项目经验 > 一、CI/CD 平台｜主导" in paths
    assert "张三 > 项目经验 > 二、资信平台｜技术经理" in paths
    # 比层级上限更深的 #### 当作正文
    cicd = next(s for s in secs if s.path[-1].startswith("一、"))
    assert any(u.kind == "heading" and "技术要点" in u.text for u in cicd.units)
    # --- 分隔线不是单元
    assert all(u.text != "---" for s in secs for u in s.units)


def test_parents_do_not_cross_projects() -> None:
    parents, _ = _parents(RESUME, min_length=0)
    by_path = {path_text(p.path): p for p in parents}
    a = by_path["张三 > 项目经验 > 一、CI/CD 平台｜主导"]
    b = by_path["张三 > 项目经验 > 二、资信平台｜技术经理"]
    assert a.content.startswith("### 一、CI/CD 平台") and "资信" not in a.content
    assert b.content.startswith("### 二、资信平台") and "Maven" not in b.content


def test_long_section_split_keeps_path() -> None:
    body = "\n\n".join(f"第{i}段：" + "这是一段很长的项目描述。" * 12 for i in range(12))
    text = f"## 项目经验\n\n### 六、AI 协同办公平台\n\n{body}\n\n## 教育背景\n\n本科\n"
    parents, _ = _parents(text, max_length=600)
    ai = [p for p in parents if p.path[-1:] == ["六、AI 协同办公平台"]]
    assert len(ai) > 1 and all(len(p.content) <= 600 for p in ai)
    # 只有第一块带标题行；后面几块靠标题路径知道自己是谁
    assert ai[0].content.startswith("### 六、") and not ai[1].content.startswith("###")


def test_short_siblings_merge_but_not_across_parents() -> None:
    text = "## 联系方式\n\n### 电话\n\n123\n\n### 邮箱\n\na@b.c\n\n## 教育\n\n本科\n"
    parents, _ = _parents(text, min_length=200)
    paths = [path_text(p.path) for p in parents]
    # 电话 + 邮箱 是同一上级下的短小节 → 合并，路径取共同上级；教育在另一个上级下，不合并
    assert paths == ["联系方式", "教育"]
    assert "### 电话" in parents[0].content and "### 邮箱" in parents[0].content


def test_children_and_context() -> None:
    parents, opts = _parents(RESUME, min_length=0)
    skills = next(p for p in parents if p.path[-1] == "专业技能")
    kids = child_texts(skills, opts)
    # 表格每行一个子块，原文只有这一行；表头在前缀里
    assert [k for k, _ in kids] == [
        "| 后端 | Java、Spring Boot、Python |",
        "| 中间件 | Kafka、Redis |",
    ]
    assert all(ctx == "张三 > 专业技能\n| 方向 | 技能 |" for _, ctx in kids)
    adv = next(p for p in parents if p.path[-1] == "个人优势")
    kids = child_texts(adv, opts)
    # 单独一行的「**核心能力**」太短，并进下一个单元
    assert kids[0][0].startswith("**核心能力**\n- 从 0 到 1")
    assert all(ctx == "张三 > 个人优势" for _, ctx in kids)


def test_chinese_numbering_headings() -> None:
    text = (
        "第一章 总则\n\n本制度适用于全体员工。\n\n"
        "第一条 目的\n\n为规范差旅报销制定本制度。\n\n"
        "第二章 报销标准\n\n第二条 住宿\n\n一线城市每晚不超过 600 元。\n"
    )
    secs = parse_sections(text, 3)
    assert ["第二章 报销标准", "第二条 住宿"] in [s.path for s in secs]


def test_no_headings_falls_back_to_paragraphs() -> None:
    text = "\n\n".join("段落内容" * 30 for _ in range(10))
    parents, _ = _parents(text, max_length=500)
    assert len(parents) > 1 and all(p.path == [] for p in parents)
    assert all(len(p.content) <= 500 for p in parents)


def test_code_block_kept_whole() -> None:
    text = "## 示例\n\n```python\n# 不是标题\ndef f():\n    return 1\n```\n"
    secs = parse_sections(text, 3)
    assert [s.path for s in secs] == [["示例"]]
    assert secs[0].units[0].kind == "code" and "# 不是标题" in secs[0].units[0].text


def test_transform_structure_modes() -> None:
    _, rules = normalize_process_rule(
        "hierarchical", None, doc_form="hierarchical_model", max_length=4000
    )
    assert rules["parent_mode"] == "structure"  # 父子分段默认按标题
    parents = transform([Document(RESUME)], doc_form="hierarchical_model", rules=rules)
    p = next(p for p in parents if p.metadata["title_path"][-1] == "二、资信平台｜技术经理")
    assert p.children and p.children[0].metadata["context"].endswith("二、资信平台｜技术经理")

    _, text_rules = normalize_process_rule(
        "structure", {"structure": {"max_length": 800}}, doc_form="text_model", max_length=4000
    )
    segs = transform([Document(RESUME)], doc_form="text_model", rules=text_rules)
    assert all(s.children is None for s in segs)
    # 通用分段默认合并 200 字以下的同级短小节：标题行留在正文里，路径取共同上级
    skills = next(s for s in segs if "## 专业技能" in s.page_content)
    assert skills.metadata["title_path"] == ["张三"]
