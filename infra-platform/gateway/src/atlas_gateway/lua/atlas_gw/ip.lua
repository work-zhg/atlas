-- 网段匹配（登录失败封禁的白名单）：IPv4 / IPv6 的 CIDR。白名单只在要封、要记的时候才查，不在热路径上。
local bit = require "bit"

local _M = {}

local function v4(s)
  local a, b, c, d = s:match("^(%d+)%.(%d+)%.(%d+)%.(%d+)$")
  if not a then
    return nil
  end
  local out = {}
  for i, p in ipairs({ a, b, c, d }) do
    local n = tonumber(p)
    if n > 255 then
      return nil
    end
    out[i] = n
  end
  return out
end

local function v6(s)
  if not s:find(":", 1, true) then
    return nil
  end
  -- 末尾是 IPv4（::ffff:1.2.3.4）：先换成两组十六进制
  local head, tail4 = s:match("^(.*:)(%d+%.%d+%.%d+%.%d+)$")
  if head then
    local b = v4(tail4)
    if not b then
      return nil
    end
    s = string.format("%s%x:%x", head, b[1] * 256 + b[2], b[3] * 256 + b[4])
  end
  local dbl = s:find("::", 1, true)
  if dbl and s:find("::", dbl + 1, true) then
    return nil
  end
  local left, right = s, ""
  if dbl then
    left, right = s:sub(1, dbl - 1), s:sub(dbl + 2)
  end
  local function groups(part)
    local g = {}
    if part == "" then
      return g
    end
    for h in (part .. ":"):gmatch("([^:]*):") do
      if not h:match("^%x%x?%x?%x?$") then
        return nil
      end
      g[#g + 1] = tonumber(h, 16)
    end
    return g
  end
  local l, r = groups(left), groups(right)
  if not l or not r then
    return nil
  end
  local n = #l + #r
  if (dbl and n > 7) or (not dbl and n ~= 8) then
    return nil
  end
  local words = {}
  for _, x in ipairs(l) do
    words[#words + 1] = x
  end
  for _ = 1, 8 - n do
    words[#words + 1] = 0
  end
  for _, x in ipairs(r) do
    words[#words + 1] = x
  end
  local out = {}
  for i, w in ipairs(words) do
    out[2 * i - 1] = bit.rshift(w, 8)
    out[2 * i] = bit.band(w, 0xff)
  end
  return out
end

-- "1.2.3.4" / "::1" → 字节数组（4 或 16 个），不合法返回 nil
function _M.parse(s)
  return v4(s) or v6(s)
end

-- ["10.0.0.0/8", "2001:db8::/32", "203.0.113.7"] → 编译好的网段；有不合法的直接报错（渲染时已校验过）
function _M.compile(list)
  local nets = {}
  for _, cidr in ipairs(list or {}) do
    local addr, len = cidr:match("^([^/]+)/(%d+)$")
    addr = addr or cidr
    local bytes = _M.parse(addr)
    if not bytes then
      error("atlas_gw: 白名单网段不合法：" .. cidr)
    end
    local bits = tonumber(len) or #bytes * 8
    if bits > #bytes * 8 then
      error("atlas_gw: 白名单网段不合法：" .. cidr)
    end
    nets[#nets + 1] = { bytes = bytes, bits = bits }
  end
  return nets
end

local function inside(bytes, net)
  if #bytes ~= #net.bytes then
    return false
  end
  local full = math.floor(net.bits / 8)
  for i = 1, full do
    if bytes[i] ~= net.bytes[i] then
      return false
    end
  end
  local rest = net.bits % 8
  if rest == 0 then
    return true
  end
  local mask = bit.band(bit.lshift(0xff, 8 - rest), 0xff)
  return bit.band(bytes[full + 1], mask) == bit.band(net.bytes[full + 1], mask)
end

function _M.match_any(nets, addr)
  if not nets or #nets == 0 then
    return false
  end
  local bytes = _M.parse(addr)
  if not bytes then
    return false
  end
  for _, net in ipairs(nets) do
    if inside(bytes, net) then
      return true
    end
  end
  return false
end

return _M
