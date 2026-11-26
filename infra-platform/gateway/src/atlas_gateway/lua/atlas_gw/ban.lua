-- LoginGuard：登录失败按 IP 封禁（c4-model/apigateway/code-auth.html）。
--   check()   http 级 rewrite_by_lua：每个请求进 location 后查 ban:<ip>，被封 → 403 IP_BLOCKED
--   record()  只挂在 login 路由的 log_by_lua：回 401 才给这个 IP 记一次；窗口内满阈值就封
-- 计数和封禁表都在共享内存 gw_ban：网关每台机器一个实例，计数是准的；reload 不丢，restart 清空。
-- ★ 只数登录接口的 401：访问令牌过期的 401 是前端刷新的正常流程，公司共用出口 IP 时数进去会封掉整个办公室。
-- ★ 登录成功不清零：否则夹一次正确登录就能从头再来。
local errors = require "atlas_gw.errors"
local ip = require "atlas_gw.ip"

local dict = ngx.shared.gw_ban
local _M = {}
local policy = { window = 600, threshold = 20, duration = 1800 }
local allow = {}

function _M.init(p)
  policy = p
  allow = ip.compile(p.allow)
end

local function allowed(addr)
  return ip.match_any(allow, addr)
end

function _M.check()
  local addr = ngx.var.remote_addr
  local until_ts = dict:get("ban:" .. addr)
  if not until_ts or allowed(addr) then
    return
  end
  local left = math.max(1, math.ceil((until_ts - ngx.now()) / 60))
  return errors.exit(
    403,
    "IP_BLOCKED",
    string.format("这个网络登录失败的次数太多，已暂时封禁，约 %d 分钟后再试", left)
  )
end

function _M.record()
  if tonumber(ngx.var.status) ~= 401 then
    return
  end
  local addr = ngx.var.remote_addr
  if allowed(addr) then
    return
  end
  -- 计数从第一次失败算起，窗口到了自动清掉（incr 不会续期已有的 key）
  local n, err = dict:incr("fail:" .. addr, 1, 0, policy.window)
  if not n then
    ngx.log(ngx.ERR, "atlas_gw ban: 记失败次数出错：", err)
    return
  end
  if n >= policy.threshold then
    local until_ts = ngx.now() + policy.duration
    dict:set("ban:" .. addr, until_ts, policy.duration)
    dict:delete("fail:" .. addr)
    ngx.log(ngx.WARN, "atlas_gw ban: ", addr, " 登录失败 ", n, " 次，封到 ", ngx.http_time(math.floor(until_ts)))
  end
end

-- 本机运维：查看、解封
function _M.list()
  local out = {}
  local now = ngx.now()
  for _, key in ipairs(dict:get_keys(0)) do
    local addr = key:match("^ban:(.+)$")
    if addr then
      local until_ts = dict:get(key)
      if until_ts then
        out[#out + 1] = {
          ip = addr,
          ["until"] = ngx.http_time(math.floor(until_ts)),
          minutes_left = math.max(0, math.ceil((until_ts - now) / 60)),
        }
      end
    end
  end
  return out
end

function _M.unban(addr)
  local was = dict:get("ban:" .. addr) ~= nil
  dict:delete("ban:" .. addr)
  dict:delete("fail:" .. addr)
  return was
end

return _M
