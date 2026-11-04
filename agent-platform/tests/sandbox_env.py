"""沙箱组件的用例共用的环境：已发布的模板、指向假 OpenSandbox 的基座。

published_template(server)   建一个已发布的模板（结束时连同它的实例登记一起删）
make_runtime(fake, redis, servers)   OpenSandboxRuntime：server 名 → 假 lifecycle
simple_spec(template)        一个最小的 CreateSpec
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

from atlas_agent.db.models import SandboxInstance, SandboxTemplate
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.sandbox.base.lifecycle.runtime import OpenSandboxRuntime
from atlas_agent.sandbox.base.lifecycle.server import LifecycleServer
from atlas_agent.sandbox.base.lifecycle.spec import CreateSpec, Labels, Resources
from atlas_agent.sandbox.base.network.policy import NetworkPolicy
from pydantic import SecretStr
from sqlalchemy import delete

from tests.opensandbox_fake import LIFECYCLE_URL, FakeOpenSandbox

ADMIN = UUID(int=7)
TEMPLATE_YAML = """
spec:
  template:
    spec:
      securityContext:
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: sandbox
"""


@asynccontextmanager
async def published_template(
    code: str | None = None, *, server: str | None = None
) -> AsyncIterator[SandboxTemplate]:
    code = code or f"t-{uuid4().hex[:8]}"
    row = SandboxTemplate(
        code=code,
        description="",
        server=server or f"srv-{code}",
        yaml=TEMPLATE_YAML,
        published_version=1,
        published_sha="x",
        updated_by_uuid=ADMIN,
    )
    async with get_sessionmaker()() as session:
        session.add(row)
        await session.commit()
    try:
        yield row
    finally:
        async with get_sessionmaker()() as session:
            await session.execute(delete(SandboxTemplate).where(SandboxTemplate.code == code))
            await session.execute(
                delete(SandboxInstance).where(SandboxInstance.template_uuid == row.template_uuid)
            )
            await session.commit()


def make_runtime(
    fake: FakeOpenSandbox, redis: Any, servers: Mapping[str, str] | None = None, **kw: Any
) -> OpenSandboxRuntime:
    """servers：server 名 → lifecycle 的地址（默认都指向 fake）。"""
    clients = {
        name: LifecycleServer(name, url, SecretStr("k"), transport=fake.async_transport)
        for name, url in (servers or {}).items()
    }
    return OpenSandboxRuntime(clients, get_sessionmaker(), redis, settle_poll_s=0.01, **kw)


def runtime_for(
    fake: FakeOpenSandbox, redis: Any, *templates: SandboxTemplate, **kw: Any
) -> OpenSandboxRuntime:
    return make_runtime(fake, redis, {t.server: LIFECYCLE_URL for t in templates}, **kw)


def simple_spec(template: str, *, image: str = "img", **labels: str) -> CreateSpec:
    return CreateSpec(
        template=template,
        image=image,
        entrypoint=("tail", "-f", "/dev/null"),
        network=NetworkPolicy.allow_all(),
        resources=Resources("100m", "2", "512Mi", "4Gi"),
        labels=Labels.of(**labels),
    )
