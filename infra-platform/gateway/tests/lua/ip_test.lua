-- resty 跑的 Lua 单元测试：网段匹配（登录失败封禁的白名单）。全部通过时最后一行打印 ok。
local ip = require "atlas_gw.ip"

local function check(cond, msg)
  if not cond then
    error("失败：" .. msg, 2)
  end
end

local nets = ip.compile({ "10.0.0.0/8", "192.168.1.7", "172.16.0.0/12", "2001:db8::/32", "::1", "fe80::/10" })
local cases = {
  { "10.1.2.3", true }, { "11.0.0.1", false }, { "192.168.1.7", true }, { "192.168.1.8", false },
  { "172.31.255.255", true }, { "172.32.0.1", false },
  { "2001:db8:1::5", true }, { "2001:db9::1", false }, { "::1", true }, { "::2", false },
  { "fe80::1", true }, { "febf:ffff::1", true }, { "fec0::1", false },
  { "not-an-ip", false }, { "1.2.3", false }, { "256.1.1.1", false },
}
for _, c in ipairs(cases) do
  check(ip.match_any(nets, c[1]) == c[2], c[1])
end
check(#ip.parse("::ffff:10.1.2.3") == 16, "IPv4 映射地址")
check(ip.parse("1::2::3") == nil, "两个 ::")
check(ip.parse("1:2:3:4:5:6:7:8:9") == nil, "九组")
check(ip.match_any({}, "10.0.0.1") == false, "空白名单")
check(not pcall(ip.compile, { "10.0.0.0/33" }), "前缀太长")
print("ok")
