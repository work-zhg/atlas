"""沙箱模板：进数据库，按 code 管理；发布 = 写 ConfigMap + 滚动重启绑定的 lifecycle server。

c4-model/agent/sandbox/code-sandbox-base-lifecycle.html。

    SandboxTemplates    管理入口：新建、修订、发布、查询。提交前经 TemplatePolicy 校验；修订只改
                        数据库、版本加一，发布后才对新建的沙箱生效
    TemplatePolicy      准入规则：拒绝特权、宿主命名空间和 hostPath，要求 seccomp 和名为 sandbox 的
                        主容器，不加 sidecar
    TemplatePublisher   写 ConfigMap opensandbox-{server}-template，改 Deployment 上的模板哈希注解
                        触发滚动重启，等新 Pod 就绪；也能读回 server 上生效内容的哈希

★ 一个模板绑定一个 lifecycle server：OpenSandbox 1.1.1 的创建请求不能选模板，模板文件由 server
  在启动时读一次。已建沙箱的 Pod spec 在建 BatchSandbox 时就定了，发布不影响它们。
★ 模板决定 Pod 的 securityContext：能改模板就能建特权 Pod、挂宿主目录，等于拿到节点。所以发布前
  强制校验，K8s 权限也收到最小。
★ 模板不留历史版本：回滚就是用旧内容再修订一次，版本号照样加一。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

import yaml  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import AsyncSession

from ....db.models import SandboxTemplate, template_sha
from ....repositories.sandbox import TemplateRepository
from .kube import KubeApi, KubeError
from .spec import TEMPLATE_CODE

if TYPE_CHECKING:
    from ....config import Settings

__all__ = [
    "SHA_ANNOTATION",
    "KubeTemplatePublisher",
    "SandboxTemplates",
    "TemplateConflict",
    "TemplateNotFound",
    "TemplatePolicy",
    "TemplatePublisher",
    "TemplateRejected",
    "template_publisher",
]

logger = logging.getLogger(__name__)

#: lifecycle server 的 Pod 注解：模板内容的哈希（前 16 位）。变了 Deployment 就滚动重启
SHA_ANNOTATION = "atlas/template-sha"


class TemplateRejected(ValueError):
    """模板不合规（TemplatePolicy）。"""


class TemplateNotFound(LookupError):
    pass


class TemplateConflict(ValueError):
    """code 或 server 已经被别的模板占用。"""


class TemplatePolicy:
    """模板的准入规则。模板里多写的容器会被 OpenSandbox 覆盖，也一并拒绝。"""

    def check(self, text: str) -> None:
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise TemplateRejected(f"模板不是合法的 YAML：{exc}") from exc
        pod = _dig(doc, "spec", "template", "spec")
        if not isinstance(pod, dict):
            raise TemplateRejected("模板要有 spec.template.spec（BatchSandbox 的 Pod 模板）")
        for key in ("hostNetwork", "hostPID", "hostIPC"):
            if pod.get(key):
                raise TemplateRejected(f"模板不能用宿主命名空间：{key}")
        for vol in pod.get("volumes") or []:
            if isinstance(vol, dict) and "hostPath" in vol:
                raise TemplateRejected(f"模板不能挂宿主目录：卷 {vol.get('name')!r} 是 hostPath")
        if pod.get("initContainers"):
            raise TemplateRejected("模板不能加 initContainers：会被 OpenSandbox 覆盖")
        containers = pod.get("containers") or []
        names = [c.get("name") for c in containers if isinstance(c, dict)]
        if names != ["sandbox"]:
            raise TemplateRejected(
                f"模板只能有一个名为 sandbox 的主容器，不能加 sidecar（现在是 {names}）"
            )
        for where, ctx in _security_contexts(pod):
            if ctx.get("privileged"):
                raise TemplateRejected(f"模板不能用特权容器：{where}")
        if not _has_seccomp(pod):
            raise TemplateRejected("模板要设 seccompProfile（Pod 级或 sandbox 容器上）")


def _dig(doc: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(doc, dict):
            return None
        doc = doc.get(key)
    return doc


def _security_contexts(pod: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(ctx := pod.get("securityContext"), dict):
        yield "Pod", ctx
    for c in pod.get("containers") or []:
        if isinstance(c, dict) and isinstance(ctx := c.get("securityContext"), dict):
            yield f"容器 {c.get('name')}", ctx


def _has_seccomp(pod: dict[str, Any]) -> bool:
    return any(isinstance(ctx.get("seccompProfile"), dict) for _, ctx in _security_contexts(pod))


class TemplatePublisher(Protocol):
    async def publish(self, template: SandboxTemplate) -> None: ...
    async def live_sha(self, server: str) -> str | None: ...


class KubeTemplatePublisher:
    """写到 K8s：ConfigMap opensandbox-{server}-template，和 Deployment 的哈希注解。

    ★ 与 deploy/local/opensandbox.sh 的 install_server 同一套命名：首次部署由脚本建 ConfigMap，
      之后由这里发布；脚本重跑时只在 ConfigMap 不在时才写，不会盖掉发布过的模板。
    """

    def __init__(
        self,
        api: KubeApi,
        *,
        namespace: str = "opensandbox-system",
        rollout_timeout_s: float = 300.0,
        poll_s: float = 2.0,
    ) -> None:
        self._api = api
        self._ns = namespace
        self._timeout = rollout_timeout_s
        self._poll = poll_s

    async def publish(self, template: SandboxTemplate) -> None:
        server, sha = template.server, template.sha()
        await self._api.merge_patch(
            f"/api/v1/namespaces/{self._ns}/configmaps/opensandbox-{server}-template",
            {"data": {f"{server}.yaml": template.yaml}},
        )
        deploy = f"/apis/apps/v1/namespaces/{self._ns}/deployments/opensandbox-{server}"
        await self._api.merge_patch(
            deploy,
            {"spec": {"template": {"metadata": {"annotations": {SHA_ANNOTATION: sha[:16]}}}}},
        )
        await self._rolled_out(deploy)

    async def live_sha(self, server: str) -> str | None:
        try:
            cm = await self._api.get(
                f"/api/v1/namespaces/{self._ns}/configmaps/opensandbox-{server}-template"
            )
        except KubeError as exc:
            if exc.status == 404:
                return None
            raise
        text = (cm.get("data") or {}).get(f"{server}.yaml")
        return template_sha(text) if text is not None else None

    async def _rolled_out(self, path: str) -> None:
        """等滚动重启完成：新一代的 Pod 全部就绪。"""
        deadline = time.monotonic() + self._timeout
        while True:
            d = await self._api.get(path)
            spec, status = d.get("spec") or {}, d.get("status") or {}
            want = int(spec.get("replicas", 1))
            if (
                int(status.get("observedGeneration", 0))
                >= int((d.get("metadata") or {}).get("generation", 0))
                and int(status.get("updatedReplicas", 0)) == want
                and int(status.get("availableReplicas", 0)) == want
                and int(status.get("replicas", 0)) == want
            ):
                return
            if time.monotonic() > deadline:
                raise KubeError(f"{path} 在 {self._timeout:.0f} 秒内没有滚动完成")
            await asyncio.sleep(self._poll)


def template_publisher(settings: Settings) -> TemplatePublisher | None:
    """集群里才能发布（要 ServiceAccount）；不在集群里返回 None，模板照样能查、能修订。"""
    try:
        api = KubeApi.in_cluster()
    except KubeError:
        logger.info("不在集群里：沙箱模板可以查、修订，不能发布")
        return None
    return KubeTemplatePublisher(api, namespace=settings.opensandbox_namespace)


class SandboxTemplates:
    """模板管理的入口。请求作用域：一个 AsyncSession，由调用方提交。"""

    def __init__(
        self,
        session: AsyncSession,
        publisher: TemplatePublisher | None,
        *,
        policy: TemplatePolicy | None = None,
    ) -> None:
        self._session = session
        self._repo = TemplateRepository(session)
        self._publisher = publisher
        self._policy = policy or TemplatePolicy()

    async def create(
        self, code: str, description: str, server: str, text: str, *, by: UUID
    ) -> SandboxTemplate:
        if not TEMPLATE_CODE.match(code):
            raise TemplateRejected(f"模板 code 只能含小写字母、数字和 -：{code!r}")
        if not TEMPLATE_CODE.match(server):
            raise TemplateRejected(f"server 名只能含小写字母、数字和 -：{server!r}")
        self._policy.check(text)
        if await self._repo.get(code) is not None:
            raise TemplateConflict(f"模板 {code} 已经存在")
        if any(t.server == server for t in await self._repo.list()):
            raise TemplateConflict(f"lifecycle server {server} 已经绑定了别的模板")
        template = SandboxTemplate(
            code=code, description=description, server=server, yaml=text, updated_by_uuid=by
        )
        self._repo.add(template)
        await self._session.flush()
        return template

    async def revise(
        self, code: str, text: str, description: str | None = None, *, by: UUID
    ) -> int:
        template = await self._get(code)
        self._policy.check(text)
        return template.revise(text, description, by=by)

    async def publish(self, code: str, *, by: UUID) -> SandboxTemplate:
        """写到绑定的 server 并等它滚动完成，再记下 published。之后新建的沙箱用这一版。"""
        if self._publisher is None:
            raise KubeError("这个部署没有配置模板发布（不在集群里）")
        template = await self._get(code)
        self._policy.check(template.yaml)  # 规则可能比入库时更严
        version, sha = template.version, template.sha()
        await self._publisher.publish(template)
        template.mark_published(version, sha, at=datetime.now(UTC), by=by)
        return template

    async def get(self, code: str) -> SandboxTemplate:
        return await self._get(code)

    async def list(self) -> list[SandboxTemplate]:
        return await self._repo.list()

    async def drifted(self, code: str) -> bool | None:
        """server 上生效的内容是不是被手工改过（和已发布的哈希对不上）。

        没发布过或查不到返回 None。
        """
        template = await self._get(code)
        if self._publisher is None or template.published_sha is None:
            return None
        live = await self._publisher.live_sha(template.server)
        return None if live is None else live != template.published_sha

    async def _get(self, code: str) -> SandboxTemplate:
        template = await self._repo.get(code)
        if template is None:
            raise TemplateNotFound(f"沙箱模板 {code} 不存在")
        return template
