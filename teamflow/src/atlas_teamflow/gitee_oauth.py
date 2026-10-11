"""成员绑定 Gitee 账号：OAuth 授权码流程，只为证明「这个 Gitee 账号是我的」
（产物仓库与 Agent 提交 · §7）。

★ 拿到用户信息后立即丢弃 access_token，不保存任何令牌；scope 只要 user_info。
★ 回调落在前端页面（gitee_oauth_redirect_uri），前端把 code / state 交给后端：
  后端不必知道自己的对外地址。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from .errors import Invalid, Upstream
from .settings import TFSettings

__all__ = ["GiteeOAuth", "GiteeUser"]

_AUTHORIZE = "https://gitee.com/oauth/authorize"
_TOKEN = "https://gitee.com/oauth/token"


@dataclass(frozen=True)
class GiteeUser:
    id: str
    login: str
    name: str | None


class GiteeOAuth:
    def __init__(
        self, settings: TFSettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.client_id = settings.gitee_oauth_client_id
        self._secret = settings.gitee_oauth_client_secret.get_secret_value()
        self.redirect_uri = settings.gitee_oauth_redirect_uri
        self._api = settings.gitee_api.rstrip("/")
        # gitee.com 直连，不走环境里的代理
        self._http = httpx.AsyncClient(timeout=15, trust_env=False, transport=transport)

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self._secret and self.redirect_uri)

    async def aclose(self) -> None:
        await self._http.aclose()

    def authorize_url(self, state: str) -> str:
        q = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "user_info",
            "state": state,
        }
        return f"{_AUTHORIZE}?{urlencode(q)}"

    async def exchange(self, code: str) -> GiteeUser:
        """授权码 → 用户信息。令牌只在这个函数里用一次。"""
        try:
            r = await self._http.post(
                _TOKEN,
                params={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": self.client_id,
                    "redirect_uri": self.redirect_uri,
                },
                data={"client_secret": self._secret},
            )
            if r.status_code >= 400:
                raise Invalid("GITEE_OAUTH_FAILED", "Gitee 授权失败或已过期，请重新绑定")
            token = r.json().get("access_token")
            if not token:
                raise Invalid("GITEE_OAUTH_FAILED", "Gitee 没有返回授权令牌，请重新绑定")
            u = await self._http.get(
                f"{self._api}/user", headers={"Authorization": f"token {token}"}
            )
            if u.status_code >= 400:
                raise Invalid("GITEE_OAUTH_FAILED", "读取 Gitee 用户信息失败，请重新绑定")
            data = u.json()
        except httpx.HTTPError as exc:
            raise Upstream("GITEE_UNAVAILABLE", f"连不上 Gitee：{type(exc).__name__}") from exc
        return GiteeUser(id=str(data["id"]), login=str(data["login"]), name=data.get("name"))
