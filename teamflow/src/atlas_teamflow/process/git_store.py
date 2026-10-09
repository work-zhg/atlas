"""产物仓库（系统设计 §11）：每个项目一个 Git 仓库，一次提交 = 一个产物版本。

两种实现，接口相同（commit / read / web_url）：
- GiteeStore：经 Gitee API 提交（多实例部署用）。实例本地不存仓库，任何实例都能提交与读取；
- LocalGitStore：本机 git CLI（单实例开发 / 测试）。

★ 只有 TeamFlow 写仓库（服务身份），Agent 不持有仓库凭据。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote
from uuid import UUID

import httpx

from ..errors import Upstream

__all__ = ["GitStore", "GiteeStore", "LocalGitStore", "make_git_store", "safe_segment"]

log = logging.getLogger("atlas_teamflow.git")


class GitStore(Protocol):
    async def commit(
        self, team_uuid: UUID, project_uuid: UUID, path: str, content: str, message: str
    ) -> str: ...

    async def read(self, team_uuid: UUID, project_uuid: UUID, sha: str, path: str) -> str: ...

    def web_url(self, team_uuid: UUID, project_uuid: UUID, sha: str, path: str) -> str | None: ...

    async def prepare(self) -> None:
        """启动时调用：预取需要网络的信息，之后 web_url 等同步方法可直接用。"""


def safe_segment(name: str) -> str:
    """路径段：去掉路径分隔符与控制字符，保留中文。"""
    s = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", name).strip(" .-")
    return s[:60] or "untitled"


class LocalGitStore:
    """本机 git CLI。只适合单实例：仓库在本机磁盘，进程内锁拦不住其他进程。"""

    def __init__(self, root: Path, author_name: str, author_email: str) -> None:
        self.root = root
        self.author_name = author_name
        self.author_email = author_email
        self._locks: dict[str, asyncio.Lock] = {}

    def repo_path(self, team_uuid: UUID, project_uuid: UUID) -> Path:
        return self.root / str(team_uuid) / str(project_uuid)

    async def _git(self, repo: Path, *args: str, stdin: bytes | None = None) -> str:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": self.author_name,
            "GIT_AUTHOR_EMAIL": self.author_email,
            "GIT_COMMITTER_NAME": self.author_name,
            "GIT_COMMITTER_EMAIL": self.author_email,
            "GIT_TERMINAL_PROMPT": "0",
        }
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repo),
            "-c",
            "core.quotepath=off",
            "-c",
            "commit.gpgsign=false",
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        out, err = await proc.communicate(stdin)
        if proc.returncode != 0:
            raise Upstream("GIT_FAILED", f"产物仓库操作失败：{err.decode(errors='replace')[:200]}")
        return out.decode()

    async def _ensure(self, repo: Path) -> None:
        if (repo / ".git").exists():
            return
        repo.mkdir(parents=True, exist_ok=True)
        await self._git(repo, "init", "-q", "-b", "main")

    async def commit(
        self, team_uuid: UUID, project_uuid: UUID, path: str, content: str, message: str
    ) -> str:
        """写入并提交一个文件 → commit sha。同一仓库的提交在进程内串行。"""
        repo = self.repo_path(team_uuid, project_uuid)
        lock = self._locks.setdefault(str(repo), asyncio.Lock())
        async with lock:
            await self._ensure(repo)
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                content if content.endswith("\n") else content + "\n", encoding="utf-8"
            )
            await self._git(repo, "add", "--", path)
            # 内容没变也要留一个版本（如人工重新提交）：--allow-empty
            await self._git(
                repo, "commit", "-q", "--allow-empty", "-F", "-", stdin=message.encode()
            )
            return (await self._git(repo, "rev-parse", "HEAD")).strip()

    async def read(self, team_uuid: UUID, project_uuid: UUID, sha: str, path: str) -> str:
        repo = self.repo_path(team_uuid, project_uuid)
        return await self._git(repo, "show", f"{sha}:{path}")

    def web_url(self, team_uuid: UUID, project_uuid: UUID, sha: str, path: str) -> str | None:
        return None

    async def prepare(self) -> None:
        return None


class GiteeStore:
    """Gitee：每个项目一个私有仓库 {namespace}/{前缀}{项目 uuid}，经 contents API 提交。

    ★ token 放在 Authorization 头里，不进 URL（访问日志、代理日志里不会出现）。
    ★ 仓库名用项目 uuid：团队 / 项目改名不影响历史；仓库描述里写可读名称。
    ★ 写入失败（网络、5xx、并发提交冲突）重试 3 次；仍失败抛错，调用方事务回滚，人可重试。
    """

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
            raise ValueError("Gitee 后端需要 ATLAS_TF_GITEE_TOKEN")
        self.web = api.rstrip("/").removesuffix("/api/v5")
        self.namespace = namespace
        self.prefix = repo_prefix
        self._http = httpx.AsyncClient(
            base_url=api.rstrip("/"),
            headers={"Authorization": f"token {token}"},
            timeout=20.0,
            transport=transport,
            trust_env=False,  # gitee.com 直连，不走系统代理
        )
        self._login: str | None = None
        self._ready: set[str] = set()
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def prepare(self) -> None:
        """预取命名空间：任何实例都能生成产物链接，不必等它自己先提交过一次。"""
        try:
            await self._owner()
        except Exception as exc:  # Gitee 暂不可用不影响启动；首次提交时会再取
            log.warning("预取 Gitee 命名空间失败：%s", exc)

    def repo(self, project_uuid: UUID) -> str:
        return f"{self.prefix}{project_uuid.hex}"

    async def _req(self, method: str, url: str, **kw: Any) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = await self._http.request(method, url, **kw)
            except httpx.HTTPError as exc:
                last = exc
            else:
                if r.status_code < 500 and r.status_code != 409:
                    return r
                last = Upstream("GIT_FAILED", f"Gitee 返回 {r.status_code}：{r.text[:120]}")
            await asyncio.sleep(0.5 * (attempt + 1))
        raise Upstream("GIT_FAILED", f"产物仓库（Gitee）暂不可用：{last}")

    async def _owner(self) -> str:
        if self.namespace:
            return self.namespace
        if self._login is None:
            r = await self._req("GET", "/user")
            if r.status_code != 200:
                raise Upstream("GIT_FAILED", f"Gitee token 无效：{r.status_code}")
            self._login = str(r.json()["login"])
        return self._login

    async def _ensure_repo(self, team_uuid: UUID, project_uuid: UUID) -> tuple[str, str]:
        owner, repo = await self._owner(), self.repo(project_uuid)
        key = f"{owner}/{repo}"
        if key in self._ready:
            return owner, repo
        async with self._lock:
            if key in self._ready:
                return owner, repo
            r = await self._req("GET", f"/repos/{owner}/{repo}")
            if r.status_code == 404:
                body = {
                    "name": repo,
                    "description": f"AI TeamFlow 产物仓库 · 团队 {team_uuid} · 项目 {project_uuid}",
                    "private": True,
                    "auto_init": True,
                }
                # 命名空间不是 token 本人 → 组织仓库
                me = self._login or (await self._req("GET", "/user")).json().get("login")
                url = "/user/repos" if owner == me else f"/orgs/{owner}/repos"
                c = await self._req("POST", url, json=body)
                if c.status_code not in (200, 201, 422):  # 422 = 其他实例刚建好
                    raise Upstream(
                        "GIT_FAILED", f"创建 Gitee 仓库失败：{c.status_code} {c.text[:120]}"
                    )
                log.info("已创建 Gitee 仓库 %s", key)
            elif r.status_code != 200:
                raise Upstream("GIT_FAILED", f"读取 Gitee 仓库失败：{r.status_code}")
            self._ready.add(key)
        return owner, repo

    @staticmethod
    def _path(path: str) -> str:
        return quote(path, safe="/")

    async def commit(
        self, team_uuid: UUID, project_uuid: UUID, path: str, content: str, message: str
    ) -> str:
        owner, repo = await self._ensure_repo(team_uuid, project_uuid)
        url = f"/repos/{owner}/{repo}/contents/{self._path(path)}"
        body: dict[str, Any] = {
            "content": base64.b64encode(
                (content if content.endswith("\n") else content + "\n").encode()
            ).decode(),
            "message": message,
        }
        cur = await self._req("GET", url)
        existing = cur.json() if cur.status_code == 200 else []
        if isinstance(existing, dict) and existing.get("sha"):
            r = await self._req("PUT", url, json={**body, "sha": existing["sha"]})
        else:
            r = await self._req("POST", url, json=body)
        if r.status_code not in (200, 201):
            raise Upstream("GIT_FAILED", f"提交到 Gitee 失败：{r.status_code} {r.text[:160]}")
        return str(r.json()["commit"]["sha"])

    async def read(self, team_uuid: UUID, project_uuid: UUID, sha: str, path: str) -> str:
        owner, repo = await self._owner(), self.repo(project_uuid)
        r = await self._req(
            "GET", f"/repos/{owner}/{repo}/contents/{self._path(path)}", params={"ref": sha}
        )
        data = r.json() if r.status_code == 200 else None
        if not isinstance(data, dict) or "content" not in data:
            raise Upstream("GIT_FAILED", f"读取 Gitee 文件失败：{r.status_code}")
        return base64.b64decode(data["content"]).decode()

    def web_url(self, team_uuid: UUID, project_uuid: UUID, sha: str, path: str) -> str | None:
        owner = self.namespace or self._login
        if not owner:
            return None
        return f"{self.web}/{owner}/{self.repo(project_uuid)}/blob/{sha}/{self._path(path)}"


def make_git_store(settings: Any) -> GitStore:
    if settings.git_backend == "gitee":
        return GiteeStore(
            api=settings.gitee_api,
            token=settings.gitee_token.get_secret_value(),
            namespace=settings.gitee_namespace,
            repo_prefix=settings.gitee_repo_prefix,
        )
    return LocalGitStore(settings.git_root, settings.git_author_name, settings.git_author_email)
