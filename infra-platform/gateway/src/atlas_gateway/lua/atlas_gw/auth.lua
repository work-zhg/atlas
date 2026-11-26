-- Authenticator：取令牌并认证（c4-model/apigateway/code-auth.html）。
--   先读 Authorization: Bearer（手机，载体 BEARER），没有再读 Cookie uc_access（Web，载体 COOKIE）；
--   COOKIE 的写请求先校 CSRF，WebSocket 握手先校 Origin；再本地验签，得到 uid、租户。
local csrf = require "atlas_gw.csrf"
local jwt = require "atlas_gw.jwt"

local _M = {}

local SAFE = { GET = true, HEAD = true, OPTIONS = true }

-- 返回 identity，或 nil, status, code, message
function _M.authenticate(s)
  local carrier, token
  local authz = ngx.var.http_authorization
  if authz then
    token = authz:match("^[Bb][Ee][Aa][Rr][Ee][Rr]%s+(%S+)%s*$")
    if not token then
      -- 带了 Authorization 却不是 Bearer：不回退去读 Cookie，免得两个来源说法不一
      return nil, 401, "TOKEN_INVALID", "登录凭据格式不对"
    end
    carrier = "BEARER"
  else
    token = ngx.var["cookie_" .. s.jwt.access_cookie]
    if not token or token == "" then
      return nil, 401, "UNAUTHENTICATED", "请先登录"
    end
    carrier = "COOKIE"
    if not SAFE[ngx.req.get_method()] then
      if not csrf.check(s.jwt) then
        return nil, 403, "CSRF_FAILED", "请求校验失败，请刷新页面后重试"
      end
    elseif ngx.var.http_upgrade and not csrf.same_origin() then
      return nil, 403, "CSRF_FAILED", "请求校验失败，请刷新页面后重试"
    end
  end
  local claims, code = jwt.verify(token, s.jwt)
  if not claims then
    if code == "TOKEN_EXPIRED" then
      return nil, 401, code, "登录已过期"
    end
    return nil, 401, code, "登录凭据无效，请重新登录"
  end
  return { uid = claims.sub, tenant = claims.tid, mcp = claims.mcp == true, carrier = carrier }
end

return _M
