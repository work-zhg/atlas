"""租户隔离的守卫（c4-model/infra-platform/code-tenancy.html）：业务代码不能绕过自动过滤。

自动过滤挂在 ORM 上，绕过 ORM 的语句不受过滤：
  · 原生 SQL（text()）—— 业务代码里一律不许写；
  · 拿 Model.__table__ 拼的 Core 语句（批量 upsert）—— 只许出现在下面登记过的几处，而且这几处
    必须自己写上 tenant_uuid（Core 语句不经过 flush，不会自动填）。
新加一处就要在这里登记，评审时一眼看到。
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "services" / "agent" / "src" / "atlas_agent"

#: 允许用 Model.__table__ 拼 Core 语句的文件（都是批量 upsert），必须显式写 tenant_uuid
CORE_ALLOWED = {
    "repositories/run_record.py",
    "repositories/skill_usage.py",
    "browser/login.py",
}

_RAW_SQL = re.compile(r"\b(?:sa\.)?text\(\s*[\"']")


def _sources() -> list[tuple[str, str]]:
    out = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith(("migrations/", "db/models.py", "db/migration_types.py")):
            continue
        out.append((rel, path.read_text(encoding="utf-8")))
    return out


def test_no_raw_sql_in_business_code() -> None:
    hits = [rel for rel, src in _sources() if _RAW_SQL.search(src)]
    assert hits == [], f"业务代码里有原生 SQL（不受租户过滤）：{hits}"


def test_core_statements_only_where_registered_and_they_write_tenant() -> None:
    using = {rel for rel, src in _sources() if "__table__" in src}
    extra = sorted(using - CORE_ALLOWED)
    assert not extra, f"没登记的 Core 语句（不受租户过滤）：{extra}"
    for rel, src in _sources():
        if rel in CORE_ALLOWED:
            assert "tenant_uuid" in src, f"{rel} 的 Core 语句没写 tenant_uuid"
