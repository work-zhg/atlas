"""上游凭据：OAuth2 client_credentials 换来的 Token 的缓存（upstream/credentials.py）。"""

from __future__ import annotations

import httpx
from atlas_mcpgateway.upstream.credentials import TokenCache


def _issuer() -> tuple[httpx.AsyncClient, list[str]]:
    """假的 token 端点：按 client_secret 发不同的 Token；secret 不对的发不出。"""
    asked: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        asked.append(form["client_secret"])
        if form["client_secret"] != "right":
            return httpx.Response(401, json={"error": "invalid_client"})
        return httpx.Response(200, json={"access_token": "tok-of-right", "expires_in": 3600})

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), asked


async def test_a_cached_token_is_not_handed_to_a_different_secret() -> None:
    """★ client_id 不是秘密：别的租户填同样的地址和 client_id、随便写个 secret，不能拿到缓存里
    别人换来的 Token —— 要拿自己的 secret 去换（换不出来就报错）。"""
    http, asked = _issuer()
    cache = TokenCache(http)
    url = "https://idp.example/token"

    assert await cache.token(url, "shared-client", "right", None) == "tok-of-right"
    try:
        await cache.token(url, "shared-client", "guess", None)
    except httpx.HTTPStatusError:
        pass
    else:
        raise AssertionError("secret 不对也拿到了 Token")
    assert asked == ["right", "guess"]

    # 同一份凭据照常走缓存
    assert await cache.token(url, "shared-client", "right", None) == "tok-of-right"
    assert asked == ["right", "guess"]
    await http.aclose()
