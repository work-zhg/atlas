-- ErrorResponder：网关自己的拒绝统一成 JSON。
--   {code, message, request_id} 是约定的信封；另带 error.kind 和 detail，现有前端的错误解包都认得。
-- Nginx 自己的 400 / 404 / 413 / 5xx 走 errors.conf（由 atlas_gateway 渲染，同一个信封）。
local cjson = require "cjson.safe"

local _M = {}

function _M.body(code, message)
  return cjson.encode({
    code = code,
    message = message,
    request_id = ngx.var.request_id,
    error = { kind = string.lower(code), message = message },
    detail = message,
  })
end

-- 结束请求：access / rewrite 阶段都能用
function _M.exit(status, code, message)
  ngx.status = status
  ngx.header["Content-Type"] = "application/json; charset=utf-8"
  ngx.header["Cache-Control"] = "no-store"
  ngx.say(_M.body(code, message))
  return ngx.exit(status)
end

return _M
