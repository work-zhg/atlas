-- CsrfCheck：Cookie 载体的写请求要证明请求来自本站（c4-model/apigateway/code-auth.html）。
--   1. Origin 必须是本站；没有 Origin 时看 Sec-Fetch-Site，必须是 same-origin；两个都没有就拒绝。
--   2. 双提交：Cookie 里的 uc_csrf 和请求头 X-UC-CSRF 相同（等长逐字节比较，耗时不随相同前缀变化）。
-- 手机的 Bearer 不会被浏览器自动带上，不用校。WebSocket 握手（GET）只校第 1 条：浏览器给它带 Cookie，但带不了自定义头。
local bit = require "bit"

local _M = {}

function _M.same_origin()
  local origin = ngx.var.http_origin
  if origin then
    return origin == ngx.var.scheme .. "://" .. (ngx.var.http_host or "")
  end
  return ngx.var.http_sec_fetch_site == "same-origin"
end

local function equal(a, b)
  if #a ~= #b then
    return false
  end
  local diff = 0
  for i = 1, #a do
    diff = bit.bor(diff, bit.bxor(a:byte(i), b:byte(i)))
  end
  return diff == 0
end

function _M.check(s)
  if not _M.same_origin() then
    return false
  end
  local cookie = ngx.var["cookie_" .. s.csrf_cookie]
  local header = ngx.var["http_" .. s.csrf_header:lower():gsub("-", "_")]
  if not cookie or not header or cookie == "" then
    return false
  end
  return equal(cookie, header)
end

return _M
