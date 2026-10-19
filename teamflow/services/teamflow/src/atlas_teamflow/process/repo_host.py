"""产物仓库的托管平台客户端（产物仓库与 Agent 提交 · §5 §8 §9）。

★ TeamFlow **不写仓库**：产物由 Agent 在会话 Pod 里用 git CLI 提交
  （凭据由 Atlas 的 git 凭据档案挂进 Pod）。
  这里只有：建仓库、读提交 / 目录 / 文件（锁定评审版本、目录预览）、管理协作者（成员本地访问）。
  代码层面不提供写文件。
★ token 放 Authorization 头，不进 URL。gitee.com 直连，不走系统代理。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote
from uuid import UUID

import httpx

from ..errors import Invalid, Upstream

__all__ = [
    "CommitInfo",
    "GiteeHost",
    "RepoFile",
    "RepoHost",
    "RepoInfo",
    "make_repo_host",
    "safe_segment",
]

log = logging.getLogger("atlas_teamflow.repo")


def safe_segment(name: str) -> str:
    """路径段：去掉路径分隔符与控制字符，保留中文。"""
    s = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", name).strip(" .-")
    return s[:60] or "untitled"


@dataclass(frozen=True)
class RepoInfo:
    full_name: str
    web_url: str
    clone_url: str


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    message: str
    committed_at: datetime
    author_name: str
    author_email: str
    #: 托管平台账号（提交邮箱能对应到账号时才有）
    author_login: str | None
    web_url: str


@dataclass(frozen=True)
class RepoFile:
    path: str
    size: int


class RepoHost(Protocol):
    namespace: str

    async def prepare(self) -> None: ...
    async def aclose(self) -> None: ...
    def repo_name(self, project_uuid: UUID) -> str: ...
    def info(self, repo: str) -> RepoInfo: ...
    async def ensure_repo(self, repo: str, description: str) -> RepoInfo: ...
    async def commits(
        self, repo: str, *, path: str | None = None, limit: int = 20
    ) -> list[CommitInfo]: ...
    async def resolve(self, repo: str, ref: str) -> str | None: ...
    async def tree(self, repo: str, sha: str, prefix: str) -> tuple[list[RepoFile], bool]: ...
    async def read_file(self, repo: str, path: str, ref: str) -> str | None: ...
    async def collaborators(self, repo: str) -> dict[str, str]: ...
    async def set_collaborator(self, repo: str, login: str, permission: str) -> None: ...
    async def remove_collaborator(self, repo: str, login: str) -> None: ...


class GiteeHost:
    """Gitee v5。仓库建在 namespace（个人或组织）下：{namespace}/{前缀}{项目 uuid}。"""

    def __init__(
        self,
        *,
        api: str,
        token: str,
        namespace: str = "",
        repo_prefix: str = "teamflow-",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not token:
            raise ValueError("需要 ATLAS_TF_GITEE_TOKEN（仓库管理令牌）")
        self.web = api.rstrip("/").removesuffix("/api/v5")
        self.namespace = namespace
        self.prefix = repo_prefix
        self._http = httpx.AsyncClient(
            base_url=api.rstrip("/"),
            headers={"Authorization": f"token {token}"},
            timeout=20.0,
            transport=transport,
            trust_env=False,
        )
        self._login: str | None = None
        self._ready: set[str] = set()
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def prepare(self) -> None:
        """启动时取 token 本人的账号：namespace 为空时仓库建在它名下。"""
        try:
            await self._owner()
        except Exception as exc:  # Gitee 暂不可用不影响启动
            log.warning("预取 Gitee 命名空间失败：%s", exc)

    def repo_name(self, project_uuid: UUID) -> str:
        return f"{self.prefix}{project_uuid.hex}"

    def info(self, repo: str) -> RepoInfo:
        owner = self.namespace or self._login or ""
        return RepoInfo(
            full_name=f"{owner}/{repo}",
            web_url=f"{self.web}/{owner}/{repo}",
            clone_url=f"{self.web}/{owner}/{repo}.git",
        )

    async def _req(self, method: str, url: str, **kw: Any) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = await self._http.request(method, url, **kw)
            except httpx.HTTPError as exc:
                last = exc
            else:
                if r.status_code < 500:
                    return r
                last = Upstream("GIT_FAILED", f"Gitee 返回 {r.status_code}")
            await asyncio.sleep(0.5 * (attempt + 1))
        raise Upstream("GIT_FAILED", f"产物仓库（Gitee）暂不可用：{last}")

    async def _owner(self) -> str:
        if self._login is None:
            r = await self._req("GET", "/user")
            if r.status_code != 200:
                raise Upstream("GIT_FAILED", f"Gitee 令牌无效：{r.status_code}")
            self._login = str(r.json()["login"])
        return self.namespace or self._login

    async def ensure_repo(self, repo: str, description: str) -> RepoInfo:
        owner = await self._owner()
        key = f"{owner}/{repo}"
        if key not in self._ready:
            async with self._lock:
                if key not in self._ready:
                    r = await self._req("GET", f"/repos/{owner}/{repo}")
                    if r.status_code == 404:
                        url = "/user/repos" if owner == self._login else f"/orgs/{owner}/repos"
                        body = {
                            "name": repo,
                            "description": description[:200],
                            "private": True,
                            "auto_init": True,
                        }
                        c = await self._req("POST", url, json=body)
                        if c.status_code not in (200, 201, 422):  # 422 = 其他实例刚建好
                            raise Upstream(
                                "GIT_FAILED", f"创建 Gitee 仓库失败：{c.status_code} {c.text[:120]}"
                            )
                        log.info("已创建 Gitee 仓库 %s", key)
                    elif r.status_code != 200:
                        raise Upstream("GIT_FAILED", f"读取 Gitee 仓库失败：{r.status_code}")
                    self._ready.add(key)
        return self.info(repo)

    async def commits(
        self, repo: str, *, path: str | None = None, limit: int = 20
    ) -> list[CommitInfo]:
        """默认分支上的提交，新的在前（Gitee 的顺序）。"""
        owner = await self._owner()
        params: dict[str, Any] = {"per_page": min(max(limit, 1), 100)}
        if path:
            params["path"] = path
        r = await self._req("GET", f"/repos/{owner}/{repo}/commits", params=params)
        if r.status_code == 404:
            return []
        if r.status_code != 200:
            raise Upstream("GIT_FAILED", f"读取提交失败：{r.status_code}")
        out = []
        for c in r.json():
            meta = c.get("commit") or {}
            author = meta.get("author") or {}
            out.append(
                CommitInfo(
                    sha=c["sha"],
                    message=str(meta.get("message") or ""),
                    committed_at=datetime.fromisoformat(
                        str(author.get("date")).replace("Z", "+00:00")
                    ),
                    author_name=str(author.get("name") or ""),
                    author_email=str(author.get("email") or ""),
                    author_login=(c.get("author") or {}).get("login"),
                    web_url=f"{self.web}/{owner}/{repo}/commit/{c['sha']}",
                )
            )
        return out

    async def resolve(self, repo: str, ref: str) -> str | None:
        """分支名 / commit → commit sha；不存在返回 None。"""
        owner = await self._owner()
        r = await self._req("GET", f"/repos/{owner}/{repo}/commits/{quote(ref, safe='')}")
        if r.status_code in (404, 422):
            return None
        if r.status_code != 200:
            raise Upstream("GIT_FAILED", f"读取提交失败：{r.status_code}")
        return str(r.json()["sha"])

    async def tree(self, repo: str, sha: str, prefix: str) -> tuple[list[RepoFile], bool]:
        """某个 commit 下 prefix 目录里的全部文件 → (文件, 是否被平台截断)。"""
        owner = await self._owner()
        r = await self._req(
            "GET", f"/repos/{owner}/{repo}/git/trees/{sha}", params={"recursive": 1}
        )
        if r.status_code == 404:
            return [], False
        if r.status_code != 200:
            raise Upstream("GIT_FAILED", f"读取目录失败：{r.status_code}")
        data = r.json()
        files = [
            RepoFile(path=str(t["path"]), size=int(t.get("size") or 0))
            for t in data.get("tree") or []
            if t.get("type") == "blob" and str(t.get("path", "")).startswith(prefix)
        ]
        return sorted(files, key=lambda f: f.path), bool(data.get("truncated"))

    async def read_file(self, repo: str, path: str, ref: str) -> str | None:
        owner = await self._owner()
        r = await self._req(
            "GET", f"/repos/{owner}/{repo}/contents/{quote(path, safe='/')}", params={"ref": ref}
        )
        data = r.json() if r.status_code == 200 else None
        if not isinstance(data, dict) or "content" not in data:
            return None
        try:
            return base64.b64decode(data["content"]).decode()
        except UnicodeDecodeError as exc:
            raise Invalid("BINARY_FILE", "二进制文件不支持预览") from exc

    async def collaborators(self, repo: str) -> dict[str, str]:
        owner = await self._owner()
        r = await self._req("GET", f"/repos/{owner}/{repo}/collaborators", params={"per_page": 100})
        if r.status_code != 200:
            raise Upstream("GIT_FAILED", f"读取仓库成员失败：{r.status_code}")
        out = {}
        for c in r.json():
            p = c.get("permissions") or {}
            out[str(c["login"])] = (
                "admin" if p.get("admin") else "push" if p.get("push") else "pull"
            )
        return out

    async def set_collaborator(self, repo: str, login: str, permission: str) -> None:
        owner = await self._owner()
        r = await self._req(
            "PUT", f"/repos/{owner}/{repo}/collaborators/{login}", json={"permission": permission}
        )
        if r.status_code not in (200, 201, 204):
            raise Upstream(
                "GIT_FAILED", f"设置仓库成员 {login} 失败：{r.status_code} {r.text[:120]}"
            )

    async def remove_collaborator(self, repo: str, login: str) -> None:
        owner = await self._owner()
        r = await self._req("DELETE", f"/repos/{owner}/{repo}/collaborators/{login}")
        if r.status_code not in (200, 204, 404):
            raise Upstream("GIT_FAILED", f"移除仓库成员 {login} 失败：{r.status_code}")


def make_repo_host(settings: Any) -> RepoHost:
    return GiteeHost(
        api=settings.gitee_api,
        token=settings.gitee_token.get_secret_value(),
        namespace=settings.gitee_namespace,
        repo_prefix=settings.gitee_repo_prefix,
    )
