-- 网关设置：gateway.json（由 atlas_gateway 从 routes.yaml 渲染），init_by_lua 时读一次，所有 worker 共用。
--   routes  每条 auth 路由的属性（pending_password_ok），按 $gw_route 查 —— 不在 Lua 里比路径前缀
--   jwt     iss、aud、时钟偏差、Cookie 和 CSRF 的名字
--   jwks    公钥集在哪、缓存文件、刷新间隔
--   ban     登录失败封禁的窗口、阈值、时长、白名单
local cjson = require "cjson.safe"

local _M = {}
local settings

function _M.init(path)
  local f, err = io.open(path, "rb")
  if not f then
    error("atlas_gw: 读不到 " .. path .. "：" .. tostring(err))
  end
  local text = f:read("*a")
  f:close()
  local s, derr = cjson.decode(text)
  if type(s) ~= "table" then
    error("atlas_gw: " .. path .. " 不是合法的 JSON：" .. tostring(derr))
  end
  settings = s
  -- 在 master 里先把要用的模块都载入（fork 后各 worker 直接用），再做一次性的准备
  require("atlas_gw.ban").init(s.ban)
  require("atlas_gw.keyring").init(s.jwks)
  require("atlas_gw.access")
  require("atlas_gw.admin")
end

function _M.get()
  return settings
end

return _M
