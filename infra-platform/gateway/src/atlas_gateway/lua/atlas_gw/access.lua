-- AccessPhase：auth 路由的 access_by_lua 唯一入口（c4-model/apigateway/code-auth.html）。
--   没有公钥 → 503 KEYS_UNAVAILABLE（不是 401：免得前端以为要重新登录）；
--   认证 → mcp（须先改密）为真且这条路由不允许改密前使用 → 403 PASSWORD_CHANGE_REQUIRED；
--   → 把 uid、租户写进 $gw_uid、$gw_tenant，由流式转发写成 X-Uid、X-Tenant-Id。
local auth = require "atlas_gw.auth"
local config = require "atlas_gw.config"
local errors = require "atlas_gw.errors"
local keyring = require "atlas_gw.keyring"

local _M = {}

function _M.run()
  local s = config.get()
  if not keyring.ready() then
    return errors.exit(503, "KEYS_UNAVAILABLE", "登录服务暂时不可用，请稍后再试")
  end
  local id, status, code, message = auth.authenticate(s)
  if not id then
    return errors.exit(status, code, message)
  end
  ngx.var.gw_carrier = id.carrier
  local route = s.routes[ngx.var.gw_route] or {}
  if id.mcp and not route.pending_password_ok then
    return errors.exit(403, "PASSWORD_CHANGE_REQUIRED", "请先修改密码")
  end
  ngx.var.gw_uid = id.uid
  ngx.var.gw_tenant = id.tenant
end

return _M
