-- 本机监控与运维端口（默认 127.0.0.1:8081）：/readyz、/bans、/bans/<ip>。
local ban = require "atlas_gw.ban"
local cjson = require "cjson.safe"
local keyring = require "atlas_gw.keyring"

local _M = {}

local function json(status, value)
  ngx.status = status
  ngx.header["Content-Type"] = "application/json; charset=utf-8"
  ngx.say(cjson.encode(value))
  return ngx.exit(status)
end

-- 一把公钥都没有 → 503（监控据此告警；网关不在 K8s 里，没有就绪探针替它摘流）
function _M.readyz()
  local st = keyring.status()
  return json(st.ready and 200 or 503, st)
end

function _M.bans()
  if ngx.req.get_method() ~= "GET" then
    return json(405, { message = "只支持 GET" })
  end
  local list = ban.list()
  setmetatable(list, cjson.array_mt)
  return json(200, { bans = list })
end

function _M.unban(addr)
  if ngx.req.get_method() ~= "DELETE" then
    return json(405, { message = "只支持 DELETE" })
  end
  return json(200, { ip = addr, was_banned = ban.unban(addr) })
end

return _M
